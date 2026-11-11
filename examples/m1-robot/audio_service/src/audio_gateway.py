"""音频输入 Gateway 编排。

/audio/capture_pcm → 单一订阅 → 格式校验 → 20 ms 重分帧 → 5 秒环形缓冲
→ 非阻塞 Audio Bus → Dashboard / WebSocket / VAD / ASR / TSE。

ROS 回调只负责校验和入队；本模块的异步 pump 负责重分帧、缓冲、电平与分发，
模型推理/网络/磁盘永远不进入 ROS 回调线程。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from input.audio_bus import AudioBus
from input.audio_frame import AudioFrame, AudioFormat
from input.audio_level import LevelMeter
from input.rechunker import Rechunker
from input.ring_buffer import RingBuffer
from input.ros_capture import RosCaptureSubscriber

LOG = logging.getLogger(__name__)


class AudioGateway:
    VERSION = "1.3.0"

    def __init__(self, config: Dict[str, Any], runtime: Optional[Any] = None):
        cfg = (config or {}).get("input", {}) or {}
        self.config = cfg
        self.runtime = runtime

        self.topic = str(cfg.get("topic", "/audio/capture_pcm"))
        self.sample_rate = int(cfg.get("sample_rate", 16000))
        self.channels = int(cfg.get("channels", 1))
        self.sample_width = int(cfg.get("sample_width", 2))
        self.encoding = str(cfg.get("encoding", "pcm_16000_16_1"))
        self.frame_ms = int(cfg.get("frame_ms", 20))
        self.ring_buffer_ms = int(cfg.get("ring_buffer_ms", 5000))
        self.ingress_capacity = int(cfg.get("ingress_capacity", 32))
        self.level_update_hz = float(cfg.get("level_update_hz", 10))
        self.consumer_capacity = int(cfg.get("consumer_capacity", 100))
        self.pre_roll_max_ms = min(int(cfg.get("pre_roll_max_ms", 5000)), self.ring_buffer_ms)
        self.stale_after_seconds = float(cfg.get("stale_after_seconds", 1.0))
        self.mock_sessions = list(cfg.get("mock_sessions", []) or [])

        self._validate()

        self.format = AudioFormat(self.sample_rate, self.channels, self.sample_width)
        self.rechunker = Rechunker(
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=self.sample_width,
            frame_ms=self.frame_ms,
        )
        self.ring = RingBuffer(frame_ms=self.frame_ms, capacity_ms=self.ring_buffer_ms)
        self.bus = AudioBus()
        self.level = LevelMeter(
            update_hz=self.level_update_hz,
            history_seconds=float(cfg.get("level_history_seconds", 60)),
            frame_ms=self.frame_ms,
        )
        self.capture = RosCaptureSubscriber(
            {
                **cfg,
                "topic": self.topic,
                "encoding": self.encoding,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "sample_width": self.sample_width,
                "ingress_capacity": self.ingress_capacity,
            },
            on_frame=self._on_ros_packet,
            runtime=runtime,
        )

        from sessions.session_manager import SessionManager

        self.sessions = SessionManager(self)

        self._sequence = 0
        self._pump_task: Optional[asyncio.Task] = None
        self._started_at: Optional[float] = None
        self._stopping = False
        self._output_times: Deque[float] = deque(maxlen=400)
        self._last_error: Optional[Dict[str, Any]] = None
        self._error_log_at = 0.0
        self._auto_sessions: List[str] = []
        self._loop = None

    # ---- 校验 -----------------------------------------------------
    def _validate(self) -> None:
        if self.sample_rate <= 0 or self.channels <= 0 or self.sample_width != 2:
            raise ValueError("input 配置非法：需要 16-bit PCM 且采样率/声道为正数")
        if self.frame_ms <= 0 or (self.sample_rate * self.frame_ms) % 1000 != 0:
            raise ValueError("input.frame_ms 必须能整除为整数采样")
        if self.ring_buffer_ms < self.frame_ms:
            raise ValueError("input.ring_buffer_ms 不能小于一帧")
        if self.ring_buffer_ms > 30000:
            raise ValueError("input.ring_buffer_ms 上限 30000")
        if self.ingress_capacity <= 0 or self.ingress_capacity > 4096:
            raise ValueError("input.ingress_capacity 需在 1..4096")
        if not 1 <= int(self.level_update_hz) <= 50:
            raise ValueError("input.level_update_hz 需在 1..50")
        if self.pre_roll_max_ms <= 0:
            raise ValueError("input.pre_roll_max_ms 必须为正数")

    # ---- 生命周期 -------------------------------------------------
    async def start(self) -> bool:
        if self._started_at is not None:
            return True
        self._loop = asyncio.get_running_loop()
        self.bus.bind_loop(self._loop)
        self.capture.ingress.bind(self._loop)
        self._stopping = False

        started = await asyncio.to_thread(self.capture.start)
        self._started_at = time.time()
        self._pump_task = asyncio.create_task(self._pump_loop(), name="audio-ingress-pump")

        for spec in self.mock_sessions:
            processor_type = spec.get("type") if isinstance(spec, dict) else str(spec)
            if not processor_type:
                continue
            try:
                session = await self.sessions.start_session(
                    processor_type,
                    pre_roll_ms=int((spec or {}).get("pre_roll_ms", 300)),
                    policy=str((spec or {}).get("policy", "fail_on_gap")),
                    capacity=int((spec or {}).get("capacity", 100)),
                )
                self._auto_sessions.append(session["session_id"])
            except Exception:  # pragma: no cover - 调试消费者不影响主链路
                LOG.exception("自启测试消费者失败 type=%s", processor_type)

        if started:
            LOG.info(
                "音频输入 Gateway 就绪 topic=%s %d Hz/%dch/%d-bit frame=%dms ring=%dms",
                self.topic,
                self.sample_rate,
                self.channels,
                self.sample_width * 8,
                self.frame_ms,
                self.ring_buffer_ms,
            )
        else:
            LOG.error("音频输入 Gateway 未订阅成功：%s", self.capture.status().get("error"))
        return started

    async def stop(self) -> None:
        self._stopping = True
        await asyncio.to_thread(self.capture.stop)
        pump = self._pump_task
        self._pump_task = None
        if pump is not None:
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass
        await self.sessions.close()
        self.bus.close()
        remaining = self.rechunker.flush()
        self._started_at = None
        LOG.info(
            "音频输入 Gateway 已停止：未成帧残留 %d bytes，输出帧 %d",
            remaining,
            self.rechunker.output_frames,
        )

    @property
    def available(self) -> bool:
        return self.capture._started

    # ---- 入口 -----------------------------------------------------
    def _on_ros_packet(self, frame: AudioFrame) -> None:
        """ROS 回调线程：只写入入口队列。"""
        self.capture.ingress.push(frame)

    async def _pump_loop(self) -> None:
        while True:
            try:
                batch = await self.capture.ingress.get_batch()
            except asyncio.CancelledError:
                raise
            for raw in batch:
                try:
                    self._process_input(raw)
                except Exception as exc:  # pragma: no cover - 单包异常不终止输入
                    self._remember_error("ingest_error", f"{type(exc).__name__}: {exc}")

    def _process_input(self, raw: AudioFrame) -> None:
        now_ns = time.monotonic_ns()
        for pcm in self.rechunker.feed(raw.pcm):
            self._sequence += 1
            frame = AudioFrame(
                sequence=self._sequence,
                captured_at_ns=now_ns,
                source_timestamp_ns=raw.source_timestamp_ns,
                sample_rate=self.sample_rate,
                channels=self.channels,
                sample_width=self.sample_width,
                encoding=self.encoding,
                pcm=pcm,
                input_sequence=raw.input_sequence,
                discontinuity=raw.discontinuity,
            )
            self.ring.append(frame)
            self.level.update(frame)
            self._output_times.append(now_ns / 1e9)
            self.bus.publish(frame)
        if self.rechunker.last_error:
            self._remember_error("rechunker", str(self.rechunker.last_error))

    def _remember_error(self, code: str, message: str) -> None:
        self._last_error = {"code": code, "message": message, "at": round(time.time(), 3)}
        now = time.time()
        if now - self._error_log_at > 5:
            self._error_log_at = now
            LOG.warning("音频输入错误 %s: %s", code, message)

    # ---- 查询 -----------------------------------------------------
    def output_rate_hz(self, window_seconds: float = 3.0) -> float:
        times = list(self._output_times)
        if len(times) < 2:
            return 0.0
        cutoff = times[-1] - window_seconds
        recent = [t for t in times if t >= cutoff]
        if len(recent) < 2:
            return 0.0
        span = recent[-1] - recent[0]
        if span <= 0:
            return 0.0
        return round((len(recent) - 1) / span, 1)

    def capture_status(self) -> Dict[str, Any]:
        """给 TTS 发布器/健康检查用的精简输入状态。"""
        status = self.capture.status()
        return {
            "online": status["online"],
            "topic": status["topic"],
            "input_rate_hz": status["input_rate_hz"],
            "last_frame_age_ms": status["last_frame_age_ms"],
            "publishers": status["publishers"],
            "received_packets": status["received_packets"],
            "invalid_packets": status["invalid_packets"],
        }

    def status(self) -> Dict[str, Any]:
        capture = self.capture.status()
        rechunker = self.rechunker.status()
        ring = self.ring.status()
        return {
            "ok": True,
            "version": self.VERSION,
            "online": capture["online"],
            "uptime_seconds": round(time.time() - self._started_at, 1) if self._started_at else 0.0,
            "capture": {
                **capture,
                "format": self.encoding,
                "active": capture["online"],
            },
            "rechunker": rechunker,
            "ring_buffer": {
                "capacity_ms": ring["capacity_ms"],
                "available_ms": ring["available_ms"],
                "capacity_frames": ring["capacity_frames"],
                "available_frames": ring["available_frames"],
                "frame_ms": ring["frame_ms"],
                "last_sequence": ring["last_sequence"],
            },
            "consumers": self.bus.snapshot(),
            "consumer_count": self.bus.consumer_count,
            "sessions": self.sessions.snapshot(),
            "metrics": {
                "received_packets": capture["received_packets"],
                "invalid_packets": capture["invalid_packets"],
                "output_frames": self.rechunker.output_frames,
                "input_dropped": capture["ingress_dropped"],
                "published_frames": self.bus.published_frames,
                "received_bytes": capture["received_bytes"],
                "input_rate_hz": capture["input_rate_hz"],
                "output_rate_hz": self.output_rate_hz(),
            },
            "last_error": self._last_error or self._capture_error(),
        }

    def _capture_error(self) -> Optional[Dict[str, Any]]:
        status = self.capture.status()
        if status.get("last_invalid"):
            return {
                "code": "invalid_packet",
                "message": status["last_reason"],
                "at": status["last_invalid"].get("at"),
            }
        if status.get("error"):
            return {"code": "capture_unavailable", "message": status["error"]}
        return None

    def level_payload(self) -> Dict[str, Any]:
        level = self.level.snapshot(stale_after_seconds=self.stale_after_seconds)
        status = self.capture.status()
        online = status["online"]
        payload = {
            "active": bool(level["active"] and online),
            "online": online,
            "device": self.topic,
            "topic": self.topic,
            "source_label": self.config.get("source_label", "厂商 media_audio 处理后麦克风流"),
            "sample_rate_hz": self.sample_rate,
            "sample_format": f"S{self.sample_width * 8}_LE",
            "channels": self.channels,
            "encoding": self.encoding,
            "frame_ms": self.frame_ms,
            "chunk_bytes": self.rechunker.frame_bytes,
            "input_rate_hz": status["input_rate_hz"],
            "output_rate_hz": self.output_rate_hz(),
            "last_frame_age_ms": status["last_frame_age_ms"],
            "packet_count": status["received_packets"],
            "stream_clients": self._stream_clients(),
            "consumers": self.bus.consumer_count,
            "ring_available_ms": self.ring.available_ms,
            "error": None,
            **level,
        }
        payload["active"] = bool(level["active"] and online)
        if not online:
            payload["error"] = "音频输入流中断"
            payload["level_db"] = None
            payload["peak_db"] = None
        elif self._last_error:
            payload["error"] = self._last_error.get("message")
        return payload

    def _stream_clients(self) -> int:
        return sum(
            1 for consumer in self.bus.snapshot() if consumer["name"].startswith("ws-input")
        )

    def health(self) -> Dict[str, Any]:
        status = self.status()
        capture = status["capture"]
        return {
            "available": self.available,
            "online": status["online"],
            "topic": capture["topic"],
            "publishers": capture["publishers"],
            "input_rate_hz": capture["input_rate_hz"],
            "last_frame_age_ms": capture["last_frame_age_ms"],
            "output_rate_hz": self.output_rate_hz(),
            "rechunker_buffered_bytes": status["rechunker"]["buffered_bytes"],
            "ring": status["ring_buffer"],
            "consumers": status["consumer_count"],
            "consumer_details": status["consumers"],
            "received_packets": status["metrics"]["received_packets"],
            "invalid_packets": status["metrics"]["invalid_packets"],
            "input_dropped": status["metrics"]["input_dropped"],
            "output_frames": status["metrics"]["output_frames"],
            "last_error": status["last_error"],
        }

    def subscribe(self, name: str, policy: str = "latest", capacity: int = 0):
        return self.bus.subscribe(name, policy=policy, capacity=capacity or self.consumer_capacity)
