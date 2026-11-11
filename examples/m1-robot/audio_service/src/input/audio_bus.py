"""进程内非阻塞发布/订阅总线。

每个消费者拥有独立有界队列与指标，支持三种策略：

* ``latest``      只保留最新帧（Dashboard 电平、慢速 WebSocket）
* ``drop_oldest`` 队列满时丢最旧帧（持续实时处理器）
* ``fail_on_gap`` 溢出或序号断裂时产生结构化 gap 事件（ASR/TSE 会话）

任何消费者都无法反向阻塞 ROS 入口：``publish`` 只做内存入队。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional

from input.audio_frame import AudioFrame

POLICIES = ("latest", "drop_oldest", "fail_on_gap")


class AudioGapError(Exception):
    """fail_on_gap 消费者检测到音频缺口。"""

    def __init__(self, event: Dict[str, Any]):
        super().__init__(event.get("reason", "audio_gap"))
        self.event = event


class AudioSubscription:
    def __init__(self, bus: "AudioBus", name: str, policy: str, capacity: int):
        if policy not in POLICIES:
            raise ValueError(f"未知消费策略 {policy}，可选 {POLICIES}")
        self.bus = bus
        self.name = str(name)
        self.policy = policy
        self.capacity = max(1, int(capacity))
        self.created_at = time.time()

        self._queue: Deque[AudioFrame] = deque()
        self._lock = threading.Lock()
        self._closed = False
        self._gap: Optional[Dict[str, Any]] = None
        self._event: Optional[asyncio.Event] = None
        self._loop = None

        self.metrics = {
            "received_frames": 0,
            "delivered_frames": 0,
            "dropped_frames": 0,
            "duplicate_frames": 0,
            "max_queue_depth": 0,
            "gaps": 0,
        }
        self.gap_events: Deque[Dict[str, Any]] = deque(maxlen=20)
        self.last_sequence: Optional[int] = None
        self.last_delivered_sequence: Optional[int] = None
        self.last_delivered_at_ns: Optional[int] = None

    # ---- 生产者侧（Gateway 调用，可在任意线程） --------------------
    def seed(self, frames: Iterable[AudioFrame]) -> int:
        """写入 pre-roll 历史帧（保持顺序），并同步连续性序号。"""
        count = 0
        with self._lock:
            if self._closed:
                return 0
            for frame in frames or ():
                if self.last_sequence is not None and frame.sequence <= self.last_sequence:
                    continue
                self._queue.append(frame)
                self.last_sequence = frame.sequence
                count += 1
            self.metrics["max_queue_depth"] = max(self.metrics["max_queue_depth"], len(self._queue))
        if count:
            self._wake()
        return count

    def offer(self, frame: AudioFrame) -> None:
        gap_event = None
        with self._lock:
            if self._closed:
                return
            if self.policy == "fail_on_gap" and self._gap is not None:
                # 已报告缺口：会话即将结束，不再接收后续帧
                return
            if self.last_sequence is not None and frame.sequence <= self.last_sequence:
                self.metrics["duplicate_frames"] += 1
                return
            if self.last_sequence is not None and frame.sequence != self.last_sequence + 1:
                gap_event = self._make_gap(
                    "sequence_gap",
                    expected=self.last_sequence + 1,
                    received=frame.sequence,
                    dropped=max(0, frame.sequence - self.last_sequence - 1),
                )
            if gap_event is not None and self.policy == "fail_on_gap":
                self._gap = gap_event
                self.gap_events.append(gap_event)
                self.metrics["gaps"] += 1
                self.last_sequence = frame.sequence
                self._wake_locked()
                return

            self.metrics["received_frames"] += 1
            if self.policy == "latest":
                if self._queue:
                    self.metrics["dropped_frames"] += len(self._queue)
                    self._queue.clear()
                self._queue.append(frame)
            elif self.policy == "drop_oldest":
                if len(self._queue) >= self.capacity:
                    self._queue.popleft()
                    self.metrics["dropped_frames"] += 1
                self._queue.append(frame)
            else:  # fail_on_gap
                if len(self._queue) >= self.capacity:
                    gap_event = self._make_gap(
                        "queue_overflow",
                        expected=None,
                        received=frame.sequence,
                        dropped=len(self._queue),
                    )
                    self._queue.clear()
                    self._gap = gap_event
                    self.gap_events.append(gap_event)
                    self.metrics["gaps"] += 1
                    self.metrics["dropped_frames"] += gap_event["dropped"]
                    self.last_sequence = frame.sequence
                    self._wake_locked()
                    return
                self._queue.append(frame)

            depth = len(self._queue)
            if depth > self.metrics["max_queue_depth"]:
                self.metrics["max_queue_depth"] = depth
            self.last_sequence = frame.sequence
        self._wake()

    def _make_gap(self, reason: str, expected: Optional[int], received: int, dropped: int) -> Dict[str, Any]:
        return {
            "type": "audio_gap",
            "consumer": self.name,
            "policy": self.policy,
            "reason": reason,
            "expected_sequence": expected,
            "received_sequence": received,
            "dropped_frames": int(dropped),
            "at": round(time.time(), 3),
        }

    def _wake(self) -> None:
        loop = self.bus.loop or self._loop
        event = self._event
        if loop is None or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # 事件循环已关闭
            pass

    def _wake_locked(self) -> None:
        # 已在持锁上下文中：直接投递唤醒
        loop = self.bus.loop or self._loop
        event = self._event
        if loop is None or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    # ---- 消费者侧 -------------------------------------------------
    def __aiter__(self) -> "AudioSubscription":
        if self._event is None:
            self._event = asyncio.Event()
            self._loop = asyncio.get_running_loop()
        return self

    async def __anext__(self) -> AudioFrame:
        if self._event is None:
            self._event = asyncio.Event()
            self._loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self.policy == "fail_on_gap" and self._gap is not None:
                    raise AudioGapError(dict(self._gap))
                if self._queue:
                    frame = self._queue.popleft()
                    self.metrics["delivered_frames"] += 1
                    self.last_delivered_sequence = frame.sequence
                    self.last_delivered_at_ns = frame.captured_at_ns
                    if not self._queue:
                        self._event.clear()
                    return frame
                if self._closed:
                    raise StopAsyncIteration
                self._event.clear()
            await self._event.wait()

    async def wait_for(self, count: int = 1, timeout: float = 1.0) -> List[AudioFrame]:
        """便捷方法：最多等待 ``timeout`` 秒收集 ``count`` 帧。"""
        frames: List[AudioFrame] = []
        deadline = time.monotonic() + timeout
        while len(frames) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(self.__anext__(), timeout=remaining)
            except (asyncio.TimeoutError, AudioGapError):
                break
            frames.append(frame)
        return frames

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
        self.bus._unregister(self)
        self._wake()

    @property
    def closed(self) -> bool:
        return self._closed

    def lag_ms(self) -> Optional[float]:
        if self.last_delivered_at_ns is None:
            return None
        return round((time.monotonic_ns() - self.last_delivered_at_ns) / 1_000_000.0, 1)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            depth = len(self._queue)
            data = dict(self.metrics)
        return {
            "name": self.name,
            "policy": self.policy,
            "capacity": self.capacity,
            "closed": self._closed,
            "queue_depth": depth,
            "max_queue_depth": data["max_queue_depth"],
            "received_frames": data["received_frames"],
            "delivered_frames": data["delivered_frames"],
            "dropped_frames": data["dropped_frames"],
            "duplicate_frames": data["duplicate_frames"],
            "gaps": data["gaps"],
            "last_sequence": self.last_sequence,
            "last_delivered_sequence": self.last_delivered_sequence,
            "lag_ms": self.lag_ms(),
            "created_at": round(self.created_at, 3),
            "last_gap": dict(self.gap_events[-1]) if self.gap_events else None,
        }


class AudioBus:
    def __init__(self):
        self._subscriptions: Dict[str, AudioSubscription] = {}
        self._lock = threading.Lock()
        self.loop = None
        self.published_frames = 0
        self._counter = 0

    def bind_loop(self, loop) -> None:
        self.loop = loop

    def subscribe(self, name: str, policy: str = "drop_oldest", capacity: int = 100) -> AudioSubscription:
        with self._lock:
            self._counter += 1
            unique = f"{name}#{self._counter}"
            subscription = AudioSubscription(self, unique, policy, capacity)
            self._subscriptions[unique] = subscription
        return subscription

    def _unregister(self, subscription: AudioSubscription) -> None:
        with self._lock:
            self._subscriptions.pop(subscription.name, None)

    def publish(self, frame: AudioFrame) -> None:
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            self.published_frames += 1
        for subscription in subscriptions:
            try:
                subscription.offer(frame)
            except Exception:  # pragma: no cover - 单个消费者异常不影响其它消费者
                pass

    def close(self) -> None:
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription.close()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            subscriptions = list(self._subscriptions.values())
        return [subscription.stats() for subscription in subscriptions]

    @property
    def consumer_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)
