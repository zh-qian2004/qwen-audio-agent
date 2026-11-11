"""ROS 2 /audio/capture_pcm 入口订阅。

回调线程只做四件事：

1. 校验消息字段（encoding / 形状 / 位宽 / 字节数）；
2. 构造入口 ``AudioFrame``；
3. 非阻塞写入有界入口队列；
4. 更新轻量接收指标。

模型推理、网络、磁盘、WebSocket 与复杂计算全部在 ROS 回调之外完成。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

from input.audio_frame import AudioFrame, parse_encoding

LOG = logging.getLogger(__name__)


class IngressQueue:
    """线程安全、有界、非阻塞的入口队列。"""

    def __init__(self, capacity: int = 32):
        self.capacity = max(1, int(capacity))
        self._items: Deque[AudioFrame] = deque()
        self._lock = threading.Lock()
        self._loop = None
        self._wake = None
        self.dropped = 0
        self.max_depth = 0

    def bind(self, loop) -> None:
        import asyncio

        self._loop = loop
        self._wake = asyncio.Event()

    def push(self, frame: AudioFrame) -> bool:
        """ROS 回调线程调用，绝不阻塞。"""
        with self._lock:
            if len(self._items) >= self.capacity:
                # 丢最旧的一帧，保证最新音频进入 Gateway
                self._items.popleft()
                self.dropped += 1
            self._items.append(frame)
            depth = len(self._items)
            if depth > self.max_depth:
                self.max_depth = depth
            loop = self._loop
            wake = self._wake
        if loop is not None and wake is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:  # 事件循环已关闭
                pass
        return True

    async def get_batch(self) -> list:
        import asyncio

        if self._wake is None:
            self.bind(asyncio.get_running_loop())
        while True:
            with self._lock:
                batch = list(self._items)
                self._items.clear()
            if batch:
                return batch
            self._wake.clear()
            await self._wake.wait()

    def drain(self) -> list:
        with self._lock:
            batch = list(self._items)
            self._items.clear()
            return batch

    def depth(self) -> int:
        with self._lock:
            return len(self._items)


class RosCaptureSubscriber:
    """单一订阅 /audio/capture_pcm 并做格式校验的 ROS 入口。"""

    def __init__(
        self,
        config: Dict[str, Any],
        on_frame: Callable[[AudioFrame], None],
        runtime: Optional[Any] = None,
    ):
        cfg = config or {}
        self.topic = str(cfg.get("topic", "/audio/capture_pcm"))
        self.encoding = str(cfg.get("encoding", "pcm_16000_16_1"))
        self.sample_rate = int(cfg.get("sample_rate", 16000))
        self.channels = int(cfg.get("channels", 1))
        self.sample_width = int(cfg.get("sample_width", 2))
        self.qos_depth = max(1, int(cfg.get("input_qos_depth", 100)))
        self.runtime = runtime
        self.on_frame = on_frame
        self.ingress = IngressQueue(int(cfg.get("ingress_capacity", 32)))

        self._subscription = None
        self._image_cls = None
        self._started = False
        self._error: Optional[str] = None
        self._lock = threading.Lock()

        self.received_packets = 0
        self.invalid_packets = 0
        self.received_bytes = 0
        self.sequence = 0
        self.last_packet_mono: Optional[float] = None
        self.last_packet_wall: Optional[float] = None
        self.last_source_ts_ns: Optional[int] = None
        self.last_invalid: Optional[Dict[str, Any]] = None
        self.last_reason: Optional[str] = None
        self._last_bytes: Optional[int] = None
        self._last_invalid_log = 0.0
        self._intervals: Deque[float] = deque(maxlen=200)
        self._last_mono: Optional[float] = None
        self._last_source_ts_ns: Optional[int] = None

    # ---- 生命周期 -------------------------------------------------
    def start(self) -> bool:
        if self._started:
            return True
        try:
            from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
            from sensor_msgs.msg import Image
        except Exception as exc:  # pragma: no cover - 机器人上 rclpy 总是存在
            self._error = f"{type(exc).__name__}: {exc}"
            LOG.error("ROS 输入不可用：%s", self._error)
            return False

        if self.runtime is None or not self.runtime.start():
            reason = getattr(self.runtime, "error", None) if self.runtime is not None else "no runtime"
            self._error = reason or "ROS 运行时不可用"
            LOG.error("ROS 输入订阅未启动：%s", self._error)
            return False

        # 与发布者兼容：Reliable + KeepLast(100)，实机验证可用。
        qos = QoSProfile(
            depth=self.qos_depth,
            history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        try:
            subscription = self.runtime.create_subscription(
                Image, self.topic, self._on_message, qos
            )
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            LOG.error("创建 /audio/capture_pcm 订阅失败：%s", self._error)
            return False

        self._subscription = subscription
        self._image_cls = Image
        self._started = True
        self._error = None
        LOG.info(
            "ROS 输入订阅就绪 topic=%s encoding=%s qos=RELIABLE depth=%d",
            self.topic,
            self.encoding,
            self.qos_depth,
        )
        return True

    def stop(self) -> None:
        subscription = self._subscription
        self._subscription = None
        self._started = False
        if subscription is not None:
            try:
                node = getattr(self.runtime, "node", None)
                if node is not None:
                    node.destroy_subscription(subscription)
            except Exception:  # pragma: no cover
                LOG.debug("销毁输入订阅异常", exc_info=True)

    # ---- ROS 回调（保持极轻量） -----------------------------------
    def _on_message(self, message) -> None:
        now_mono = time.monotonic()
        reason = None
        fmt = parse_encoding(getattr(message, "encoding", None))
        data = bytes(getattr(message, "data", b"") or b"")

        if getattr(message, "is_bigendian", False):
            reason = "big_endian_unsupported"
        elif self._shape_mismatch(message, len(data)):
            reason = "shape_mismatch"
        elif fmt is None:
            reason = "encoding_unparsable"
        elif fmt.sample_width != self.sample_width:
            reason = f"sample_width_{fmt.sample_width * 8}bit"
        elif fmt.sample_rate != self.sample_rate:
            reason = f"sample_rate_{fmt.sample_rate}"
        elif fmt.channels != self.channels:
            reason = f"channels_{fmt.channels}"
        else:
            try:
                fmt.validate_payload(data)
            except ValueError as exc:
                reason = f"invalid_payload:{exc}"

        if reason is not None:
            self._record_invalid(reason, now_mono)
            return

        source_ts_ns = self._source_timestamp_ns(message)
        discontinuity = False
        with self._lock:
            if self._last_mono is not None:
                interval = now_mono - self._last_mono
                self._intervals.append(interval)
                # 输入约 15.6 Hz；超过 3 个包间隔视为源中断
                if interval > 0.2:
                    discontinuity = True
            if (
                source_ts_ns is not None
                and self._last_source_ts_ns is not None
                and source_ts_ns <= self._last_source_ts_ns
            ):
                discontinuity = True
            self._last_mono = now_mono
            if source_ts_ns is not None:
                self._last_source_ts_ns = source_ts_ns

            self.sequence += 1
            frame = AudioFrame(
                sequence=0,  # Gateway 重分帧后统一编号
                captured_at_ns=time.monotonic_ns(),
                source_timestamp_ns=source_ts_ns,
                sample_rate=fmt.sample_rate,
                channels=fmt.channels,
                sample_width=fmt.sample_width,
                encoding=fmt.encoding,
                pcm=data,
                input_sequence=self.sequence,
                discontinuity=discontinuity,
            )
            self.received_packets += 1
            self.received_bytes += len(data)
            self._last_bytes = len(data)
            self.last_packet_mono = now_mono
            self.last_packet_wall = time.time()
            self.last_source_ts_ns = source_ts_ns

        try:
            self.on_frame(frame)
        except Exception:  # pragma: no cover - 消费侧异常不能回传 ROS 线程
            LOG.exception("入口帧处理异常")

    def _record_invalid(self, reason: str, now_mono: float) -> None:
        with self._lock:
            self.invalid_packets += 1
            self.last_reason = reason
            self.last_invalid = {"reason": reason, "at": round(now_mono, 6)}
        # 速率限制：同一原因最多每 5 秒记录一次
        if time.time() - self._last_invalid_log > 5:
            self._last_invalid_log = time.time()
            LOG.warning("丢弃非法输入包：%s", reason)

    @staticmethod
    def _shape_mismatch(message, byte_length: int) -> bool:
        """校验 Image 形状与数据长度（厂商 height=1, width/step=字节数）。"""
        try:
            height = int(getattr(message, "height", 0) or 0)
            width = int(getattr(message, "width", 0) or 0)
            step = int(getattr(message, "step", 0) or 0)
        except Exception:
            return True
        if height and step:
            return height * step != byte_length
        if width and not step:
            return width != byte_length
        return False

    @staticmethod
    def _source_timestamp_ns(message) -> Optional[int]:
        header = getattr(message, "header", None)
        stamp = getattr(header, "stamp", None)
        if stamp is None:
            return None
        try:
            return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
        except Exception:
            return None

    # ---- 指标 -----------------------------------------------------
    def input_rate_hz(self, window_seconds: float = 3.0) -> float:
        with self._lock:
            intervals = list(self._intervals)
        if len(intervals) < 2:
            return 0.0
        max_samples = max(2, int(window_seconds * 20))
        recent = intervals[-max_samples:]
        total = sum(recent)
        if total <= 0:
            return 0.0
        return round(len(recent) / total, 2)

    def last_frame_age_ms(self) -> Optional[float]:
        with self._lock:
            last = self.last_packet_mono
        if last is None:
            return None
        return round((time.monotonic() - last) * 1000.0, 1)

    def publishers(self) -> Optional[int]:
        if self.runtime is None:
            return None
        return self.runtime.count_publishers(self.topic)

    def online(self, stale_after_seconds: float = 1.0) -> bool:
        age_ms = self.last_frame_age_ms()
        return bool(self._started and age_ms is not None and age_ms <= stale_after_seconds * 1000.0)

    def status(self) -> Dict[str, Any]:
        publishers = self.publishers()
        return {
            "online": self.online(),
            "subscribed": bool(self._started),
            "topic": self.topic,
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width": self.sample_width,
            "qos": {"reliability": "reliable", "depth": self.qos_depth, "history": "keep_last"},
            "publishers": publishers,
            "input_packet_bytes": self._last_packet_bytes(),
            "input_rate_hz": self.input_rate_hz(),
            "last_frame_age_ms": self.last_frame_age_ms(),
            "last_source_timestamp_ns": self.last_source_ts_ns,
            "received_packets": self.received_packets,
            "invalid_packets": self.invalid_packets,
            "received_bytes": self.received_bytes,
            "ingress_depth": self.ingress.depth(),
            "ingress_capacity": self.ingress.capacity,
            "ingress_dropped": self.ingress.dropped,
            "error": self._error,
            "last_reason": self.last_reason,
            "last_invalid": self.last_invalid,
        }

    def _last_packet_bytes(self) -> Optional[int]:
        return getattr(self, "_last_bytes", None) or None
