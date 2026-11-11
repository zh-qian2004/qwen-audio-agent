"""串行播放队列。

- 容量有限，超出直接拒绝（不静默丢弃）。
- 普通任务按到达顺序排队；priority=high / interrupt 的任务插入队首。
- 单 worker 消费，保证任意时刻只有一个任务向扬声器发布 PCM。
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Dict, List, Optional

from state import ServiceError, TtsTask

VALID_PRIORITIES = ("normal", "high")


class PlaybackQueue:
    def __init__(self, capacity: int = 8):
        self.capacity = max(1, int(capacity))
        self._items: deque = deque()
        self._cond = asyncio.Condition()
        self._closed = False

    @property
    def pending(self) -> int:
        """近似值，仅用于状态展示。"""
        return len(self._items)

    async def put(self, task: TtsTask, front: bool = False) -> int:
        async with self._cond:
            if self._closed:
                raise ServiceError("service_stopping", "audio_service is stopping", 503)
            if len(self._items) >= self.capacity:
                raise ServiceError(
                    "queue_full",
                    f"playback queue is full (capacity={self.capacity})",
                    429,
                    {"capacity": self.capacity, "pending": len(self._items)},
                )
            if front:
                self._items.appendleft(task)
                position = 0
            else:
                self._items.append(task)
                position = len(self._items) - 1
            self._cond.notify()
            return position

    async def get(self) -> Optional[TtsTask]:
        async with self._cond:
            while not self._items and not self._closed:
                await self._cond.wait()
            if self._items:
                return self._items.popleft()
            return None

    async def drain(self) -> List[TtsTask]:
        async with self._cond:
            items = list(self._items)
            self._items.clear()
            return items

    async def close(self) -> None:
        async with self._cond:
            self._closed = True
            self._cond.notify_all()

    async def snapshot(self) -> Dict[str, Any]:
        async with self._cond:
            return {
                "capacity": self.capacity,
                "pending": len(self._items),
                "items": [{"task_id": t.task_id, "phase": t.phase, "priority": t.priority} for t in self._items],
            }
