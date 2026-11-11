"""Shared speaker ownership arbitration for TTS and realtime PCM streams."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from state import ServiceError

LOG = logging.getLogger(__name__)

PRIORITY_TTS_NORMAL = 10
PRIORITY_REALTIME = 20
PRIORITY_TTS_HIGH = 30


@dataclass
class PlaybackLease:
    source: str
    owner_id: str
    priority: int
    metadata: Dict[str, Any]
    lease_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    acquired_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_reason: Optional[str] = None
    released: bool = False

    def cancel(self, reason: str) -> None:
        if not self.cancel_event.is_set():
            self.cancel_reason = reason
            self.cancel_event.set()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "source": self.source,
            "owner_id": self.owner_id,
            "priority": self.priority,
            "metadata": dict(self.metadata),
            "acquired_at": round(self.acquired_at, 3),
            "held_seconds": round(max(0.0, time.time() - self.acquired_at), 3),
            "cancel_requested": self.cancel_event.is_set(),
            "cancel_reason": self.cancel_reason,
        }


@dataclass
class _Waiter:
    source: str
    owner_id: str
    priority: int
    metadata: Dict[str, Any]
    sequence: int

    def snapshot(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "owner_id": self.owner_id,
            "priority": self.priority,
            "metadata": dict(self.metadata),
        }


class CombinedCancel:
    """Small Event-compatible view used by RosPcmPublisher."""

    def __init__(self, *events: asyncio.Event):
        self.events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


class PlaybackArbiter:
    """Priority-aware, exclusive ownership of the physical speaker path."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.acquire_timeout = max(0.1, float(config.get("acquire_timeout_seconds", 180.0)))
        configured_priorities = config.get("priorities", {})
        self.priorities = {
            "tts_normal": int(configured_priorities.get("tts_normal", PRIORITY_TTS_NORMAL)),
            "realtime": int(configured_priorities.get("realtime", PRIORITY_REALTIME)),
            "tts_high": int(configured_priorities.get("tts_high", PRIORITY_TTS_HIGH)),
        }
        if not self.priorities["tts_normal"] < self.priorities["realtime"] < self.priorities["tts_high"]:
            raise ValueError("playback priorities must satisfy tts_normal < realtime < tts_high")
        self._condition = asyncio.Condition()
        self._owner: Optional[PlaybackLease] = None
        self._waiters: list[_Waiter] = []
        self._sequence = 0
        self._recent: deque = deque(maxlen=max(5, int(config.get("recent_events", 20))))

    async def acquire(
        self,
        source: str,
        owner_id: str,
        priority: int,
        metadata: Optional[Dict[str, Any]] = None,
        cancel: Optional[asyncio.Event] = None,
        wait: bool = True,
    ) -> PlaybackLease:
        metadata = dict(metadata or {})
        self._sequence += 1
        waiter = _Waiter(source, owner_id, int(priority), metadata, self._sequence)
        deadline = time.monotonic() + self.acquire_timeout

        async with self._condition:
            current = self._owner
            if current is not None and waiter.priority > current.priority:
                reason = f"preempted_by:{source}:{owner_id}"
                current.cancel(reason)
                self._record("preempt_requested", current=current.snapshot(), incoming=waiter.snapshot())
                LOG.info(
                    "扬声器抢占请求 current=%s/%s incoming=%s/%s",
                    current.source,
                    current.owner_id,
                    source,
                    owner_id,
                )
            elif current is not None and not wait:
                raise ServiceError(
                    "speaker_busy",
                    "扬声器正由更高或同优先级来源占用",
                    409,
                    {"owner": current.snapshot()},
                )

            self._waiters.append(waiter)
            self._condition.notify_all()
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise ServiceError("playback_cancelled", "等待扬声器期间已取消", 409)
                    if time.monotonic() >= deadline:
                        raise ServiceError(
                            "speaker_acquire_timeout",
                            "等待扬声器超时",
                            504,
                            {"timeout_seconds": self.acquire_timeout},
                        )
                    if self._owner is None and self._is_next(waiter):
                        lease = PlaybackLease(source, owner_id, waiter.priority, metadata)
                        self._owner = lease
                        self._waiters.remove(waiter)
                        self._record("acquired", owner=lease.snapshot())
                        LOG.info("扬声器已分配 source=%s owner=%s priority=%d", source, owner_id, priority)
                        return lease
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._condition.notify_all()

    async def release(self, lease: Optional[PlaybackLease], outcome: str = "released") -> None:
        if lease is None or lease.released:
            return
        async with self._condition:
            lease.released = True
            if self._owner is lease:
                self._owner = None
                self._record(outcome, owner=lease.snapshot())
                LOG.info("扬声器已释放 source=%s owner=%s outcome=%s", lease.source, lease.owner_id, outcome)
                self._condition.notify_all()

    async def stop(self) -> None:
        async with self._condition:
            if self._owner is not None:
                self._owner.cancel("service_stopping")
            self._condition.notify_all()

    def snapshot(self) -> Dict[str, Any]:
        owner = self._owner.snapshot() if self._owner is not None else None
        waiters = sorted(self._waiters, key=lambda item: (-item.priority, item.sequence))
        return {
            "owner": owner,
            "idle": owner is None,
            "waiters": [item.snapshot() for item in waiters],
            "tts_waiting": sum(1 for item in waiters if item.source == "tts"),
            "realtime_waiting": sum(1 for item in waiters if item.source == "realtime"),
            "acquire_timeout_seconds": self.acquire_timeout,
            "policy": {
                "priorities": dict(self.priorities),
                "higher_priority_preempts": True,
                "equal_priority_realtime_busy": True,
            },
            "recent_events": list(self._recent),
        }

    def priority(self, name: str) -> int:
        return self.priorities[name]

    def _is_next(self, waiter: _Waiter) -> bool:
        if not self._waiters:
            return False
        next_waiter = min(self._waiters, key=lambda item: (-item.priority, item.sequence))
        return next_waiter is waiter

    def _record(self, event: str, **detail: Any) -> None:
        self._recent.appendleft({"event": event, "at": round(time.time(), 3), **detail})
