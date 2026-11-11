"""ROS 2 扬声器 PCM 发布器。

只通过 /audio/speaker_pcm 提交 PCM，不打开 ALSA 设备，不接触 media_audio 进程。
消息格式按厂商约定：
    sensor_msgs/msg/Image
    height=1, width=len(pcm), step=len(pcm), encoding=pcm_48000_16_2, is_bigendian=False
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from state import ServiceError

LOG = logging.getLogger(__name__)


def find_processes(name: str) -> list:
    """扫描 /proc 找出名字匹配的进程 PID（用于 media_audio 存活检查）。"""
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read().decode("utf-8", "ignore")
        except OSError:
            continue
        parts = [part for part in cmdline.split("\x00") if part]
        if not parts:
            continue
        for part in parts[:2]:
            base = os.path.basename(part)
            if base == name or base.startswith(name):
                pids.append(int(entry))
                break
    return sorted(pids)


class RosPcmPublisher:
    """只负责扬声器 PCM 发布。

    传入 ``runtime`` 时复用 audio_service 的共享 ROS Node/Executor；未传入时
    为兼容独立使用场景自行初始化 rclpy。捕获话题由输入侧 RosCaptureSubscriber
    负责订阅，本类不再创建 /audio/capture_pcm 订阅。
    """

    def __init__(
        self,
        config: Dict[str, Any],
        media_audio_process: str = "media_audio",
        runtime: Optional[Any] = None,
        capture_stats: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.node_name = str(config.get("node_name", "audio_service_publisher"))
        self.speaker_topic = str(config.get("speaker_topic", "/audio/speaker_pcm"))
        self.speaker_status_topic = str(config.get("speaker_status_topic", "/audio/speaker_pcm_status"))
        self.capture_topic = str(config.get("capture_topic", "/audio/capture_pcm"))
        self.encoding = str(config.get("encoding", "pcm_48000_16_2"))
        self.qos_depth = int(config.get("qos_depth", 10))
        self.media_audio_process = media_audio_process
        self.runtime = runtime
        self.capture_stats = capture_stats

        self._node = None
        self._publisher = None
        self._status_subscription = None
        self._image_cls = None
        self._spin_thread: Optional[threading.Thread] = None
        self._started = False
        self._owns_node = False
        self._import_error: Optional[str] = None
        self._lock = threading.Lock()

        self._speaker_status: Optional[int] = None
        self._speaker_status_at: Optional[float] = None
        self.published_packets = 0
        self.published_bytes = 0
        self.last_publish_at: Optional[float] = None
        self.last_error: Optional[str] = None

    # ---- 生命周期 -------------------------------------------------
    def start(self) -> bool:
        if self._started:
            return True
        try:
            from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
            from sensor_msgs.msg import Image
            from std_msgs.msg import Int32
        except Exception as exc:
            self._import_error = f"{type(exc).__name__}: {exc}"
            LOG.error("ROS 2 不可用：%s", self._import_error)
            return False

        qos = QoSProfile(
            depth=self.qos_depth,
            history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        node = None
        owns_node = False
        thread = None

        if self.runtime is not None:
            if not self.runtime.start():
                self._import_error = self.runtime.error or "ros runtime unavailable"
                LOG.error("共享 ROS 运行时不就绪：%s", self._import_error)
                return False
            node = self.runtime.node
        else:
            try:
                import rclpy
                from rclpy.node import Node
            except Exception as exc:
                self._import_error = f"{type(exc).__name__}: {exc}"
                LOG.error("ROS 2 不可用：%s", self._import_error)
                return False
            try:
                try:
                    rclpy.init(args=None)
                except Exception as exc:  # 已被初始化时继续使用现有 context
                    LOG.warning("rclpy.init 提示：%s", exc)
                node = Node(self.node_name)
                thread = threading.Thread(target=rclpy.spin, args=(node,), name="ros-spin", daemon=True)
                thread.start()
                owns_node = True
            except Exception as exc:
                self._import_error = f"{type(exc).__name__}: {exc}"
                LOG.error("ROS 2 节点初始化失败：%s", self._import_error)
                return False

        try:
            publisher = node.create_publisher(Image, self.speaker_topic, qos)
            status_subscription = node.create_subscription(
                Int32, self.speaker_status_topic, self._on_speaker_status, qos
            )
        except Exception as exc:
            self._import_error = f"{type(exc).__name__}: {exc}"
            LOG.error("ROS 2 发布器初始化失败：%s", self._import_error)
            if owns_node and node is not None:
                try:
                    node.destroy_node()
                except Exception:  # pragma: no cover
                    pass
            return False

        self._node = node
        self._publisher = publisher
        self._status_subscription = status_subscription
        self._image_cls = Image
        self._spin_thread = thread
        self._owns_node = owns_node
        self._started = True
        LOG.info(
            "ROS 发布器就绪 topic=%s encoding=%s qos=RELIABLE depth=%d shared_runtime=%s",
            self.speaker_topic,
            self.encoding,
            self.qos_depth,
            self.runtime is not None,
        )
        return True

    def stop(self) -> None:
        node = self._node
        status_subscription = self._status_subscription
        self._started = False
        self._publisher = None
        self._node = None
        self._status_subscription = None
        if not self._owns_node:
            # 共享运行时：只释放自己的订阅，不销毁节点/Context
            if node is not None and status_subscription is not None:
                try:
                    node.destroy_subscription(status_subscription)
                except Exception:  # pragma: no cover
                    pass
            return
        if node is not None:
            try:
                node.destroy_node()
            except Exception:  # pragma: no cover
                pass
        try:
            import rclpy

            rclpy.try_shutdown()
        except Exception:  # pragma: no cover
            pass

    # ---- 订阅回调 -------------------------------------------------
    def _on_speaker_status(self, msg) -> None:
        with self._lock:
            self._speaker_status = int(msg.data)
            self._speaker_status_at = time.time()

    def capture_status(self) -> Dict[str, Any]:
        """输入侧由 Gateway 提供，发布器只做透传，避免重复订阅。"""
        if self.capture_stats is not None:
            try:
                data = dict(self.capture_stats() or {})
            except Exception:  # pragma: no cover - 指标异常不影响 TTS
                data = {}
        else:
            data = {}
        rate = float(data.get("input_rate_hz", data.get("rate_hz", 0.0)) or 0.0)
        online = bool(data.get("online", rate > 1.0))
        return {
            "topic": data.get("topic", self.capture_topic),
            "rate_hz": round(rate, 2),
            "active": online,
        }

    def capture_rate_hz(self, window: Optional[float] = None) -> float:
        return float(self.capture_status()["rate_hz"])

    def media_audio_status(self) -> Dict[str, Any]:
        pids = find_processes(self.media_audio_process)
        return {
            "process": self.media_audio_process,
            "running": bool(pids),
            "pid": pids[0] if pids else None,
            "pids": pids,
        }

    def status(self) -> Dict[str, Any]:
        node = self._node
        subscribers = None
        publishers = None
        if node is not None:
            try:
                subscribers = node.count_subscribers(self.speaker_topic)
                publishers = node.count_publishers(self.speaker_topic)
            except Exception:  # pragma: no cover
                pass
        capture = self.capture_status()
        with self._lock:
            speaker_status = self._speaker_status
            speaker_status_at = self._speaker_status_at
        return {
            "available": bool(self._started),
            "error": self._import_error,
            "node": self.node_name,
            "topic": self.speaker_topic,
            "encoding": self.encoding,
            "qos": {"reliability": "reliable", "depth": self.qos_depth},
            "subscribers": subscribers,
            "publishers": publishers,
            "published_packets": self.published_packets,
            "published_bytes": self.published_bytes,
            "last_publish_at": round(self.last_publish_at, 3) if self.last_publish_at else None,
            "last_error": self.last_error,
            "capture": capture,
            "speaker_status": {
                "topic": self.speaker_status_topic,
                "value": speaker_status,
                "at": round(speaker_status_at, 3) if speaker_status_at else None,
            },
        }

    # ---- 发布 -----------------------------------------------------
    def publish_chunk(self, chunk: bytes) -> Dict[str, Any]:
        """Publish one already-converted 48 kHz/S16_LE/stereo PCM chunk.

        Speaker ownership is deliberately handled by PlaybackArbiter; this method
        only validates and emits one ROS message so TTS and realtime streams share
        exactly the same low-level path.
        """
        if not self._started or self._publisher is None or self._image_cls is None:
            raise ServiceError("ros_unavailable", f"ROS 发布器不可用：{self._import_error or 'not started'}", 503)
        payload = bytes(chunk)
        if not payload:
            raise ServiceError("pcm_format_invalid", "PCM payload is empty", 400)
        if len(payload) % 4:
            raise ServiceError(
                "pcm_format_invalid",
                "48 kHz 双声道 PCM 必须按 4 字节 frame 对齐",
                400,
                {"bytes": len(payload), "frame_bytes": 4},
            )

        msg = self._image_cls()
        msg.header.stamp = self._node.get_clock().now().to_msg()
        msg.header.frame_id = ""
        msg.height = 1
        msg.width = len(payload)
        msg.step = len(payload)
        msg.encoding = self.encoding
        msg.is_bigendian = False
        msg.data = payload
        self._publisher.publish(msg)
        with self._lock:
            self.published_packets += 1
            self.published_bytes += len(payload)
            self.last_publish_at = time.time()
        return {"bytes": len(payload), "topic": self.speaker_topic, "encoding": self.encoding}

    async def publish_pcm(
        self,
        pcm,
        chunk_ms: float,
        cancel: asyncio.Event,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        lead_chunks: int = 2,
    ) -> Dict[str, Any]:
        if not self._started or self._publisher is None or self._image_cls is None:
            raise ServiceError("ros_unavailable", f"ROS 发布器不可用：{self._import_error or 'not started'}", 503)

        chunks = pcm.chunks(chunk_ms)
        chunk_seconds = chunk_ms / 1000.0
        lead_chunks = max(0, int(lead_chunks))

        pid_before = self.media_audio_status()
        capture_start = self.capture_rate_hz()

        started = time.monotonic()
        sent_packets = 0
        sent_bytes = 0
        lag_events = 0
        total_frames = pcm.frames

        for index, chunk in enumerate(chunks):
            if cancel.is_set():
                break
            target = started + max(0, index - lead_chunks) * chunk_seconds
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -chunk_seconds:
                lag_events += 1

            self.publish_chunk(chunk)

            sent_packets += 1
            sent_bytes += len(chunk)
            if on_progress is not None:
                frame_bytes = pcm.frame_bytes
                sent_frames = sent_bytes // frame_bytes
                on_progress(
                    {
                        "sent_packets": sent_packets,
                        "sent_bytes": sent_bytes,
                        "remaining_seconds": round(max(0, total_frames - sent_frames) / pcm.sample_rate, 3),
                    }
                )

        elapsed = time.monotonic() - started
        pid_after = self.media_audio_status()
        capture_end = self.capture_rate_hz()
        completed = not cancel.is_set()
        stats = {
            "packets": sent_packets,
            "expected_packets": len(chunks),
            "dropped_packets": len(chunks) - sent_packets,
            "chunk_bytes": len(chunks[0]) if chunks else 0,
            "bytes": sent_bytes,
            "duration_seconds": round(elapsed, 3),
            "rate_hz": round(1.0 / chunk_seconds, 2),
            "measured_rate_hz": round(sent_packets / elapsed, 2) if elapsed > 0 else 0.0,
            "lag_events": lag_events,
            "completed": completed,
            "target": self.speaker_topic,
            "encoding": self.encoding,
            "capture_rate_hz_start": capture_start,
            "capture_rate_hz_end": capture_end,
            "media_audio_pid_before": pid_before.get("pid"),
            "media_audio_pid_after": pid_after.get("pid"),
            "media_audio_pid_unchanged": pid_before.get("pid") == pid_after.get("pid") and pid_before.get("pid") is not None,
        }
        LOG.info(
            "PCM 发布完成 packets=%d/%d chunk=%d bytes=%.3fs rate=%.2fHz lag=%d media_audio_pid=%s(%s) capture=%.2f->%.2fHz",
            sent_packets,
            len(chunks),
            stats["chunk_bytes"],
            elapsed,
            stats["measured_rate_hz"],
            lag_events,
            stats["media_audio_pid_before"],
            "unchanged" if stats["media_audio_pid_unchanged"] else "changed",
            capture_start,
            capture_end,
        )
        return stats
