"""固定容量的 20 ms 帧环形缓冲。

默认 5 秒：5 s × 50 fps = 250 帧，用于 pre-roll、ASR/TSE 上下文初始化和
WebSocket 新客户端预缓冲。写入只做 deque 追加，不会被任何消费者阻塞。
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict, List, Optional

from input.audio_frame import AudioFrame


class RingBuffer:
    def __init__(self, frame_ms: int = 20, capacity_ms: int = 5000):
        self.frame_ms = max(1, int(frame_ms))
        self.capacity_ms = max(self.frame_ms, int(capacity_ms))
        self.capacity_frames = max(1, self.capacity_ms // self.frame_ms)
        self._frames: Deque[AudioFrame] = deque(maxlen=self.capacity_frames)
        self._lock = threading.Lock()
        self.written = 0
        self.overwritten = 0

    def append(self, frame: AudioFrame) -> None:
        with self._lock:
            if len(self._frames) == self.capacity_frames:
                self.overwritten += 1
            self._frames.append(frame)
            self.written += 1

    def latest(self, count: int = 1) -> List[AudioFrame]:
        count = max(1, int(count))
        with self._lock:
            items = list(self._frames)
        return items[-count:] if items else []

    def recent(self, milliseconds: int) -> List[AudioFrame]:
        """返回最近 ``milliseconds`` 的帧（不足时返回全部已有帧）。"""
        if milliseconds <= 0:
            return []
        frames = max(1, int(milliseconds) // self.frame_ms)
        return self.latest(frames)

    def last_frame(self) -> Optional[AudioFrame]:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._frames)

    @property
    def available_ms(self) -> int:
        return self.count * self.frame_ms

    def status(self) -> Dict[str, object]:
        count = self.count
        with self._lock:
            first = self._frames[0].sequence if self._frames else None
            last = self._frames[-1].sequence if self._frames else None
        return {
            "capacity_ms": self.capacity_ms,
            "available_ms": count * self.frame_ms,
            "capacity_frames": self.capacity_frames,
            "available_frames": count,
            "frame_ms": self.frame_ms,
            "first_sequence": first,
            "last_sequence": last,
            "written": self.written,
            "overwritten": self.overwritten,
        }
