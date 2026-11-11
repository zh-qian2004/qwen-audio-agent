"""Gateway 内音频电平与活动度计算。

以约 10 Hz 更新对外电平（不随 Dashboard 请求重新遍历 PCM），
同时用 20 ms 帧的 RMS 维护 5 秒活动度/静音比例。
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from input.audio_frame import AudioFrame

FLOOR_DBFS = -96.0


def to_dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return FLOOR_DBFS
    return 20.0 * math.log10(amplitude / 32768.0)


class LevelMeter:
    def __init__(
        self,
        update_hz: float = 10.0,
        history_seconds: float = 60.0,
        active_threshold_dbfs: float = -50.0,
        activity_window_seconds: float = 5.0,
        frame_ms: int = 20,
    ):
        self.update_interval = 1.0 / max(1.0, float(update_hz))
        self.history_max = max(10, int(history_seconds * update_hz))
        self.active_threshold = float(active_threshold_dbfs)
        self.frame_ms = max(1, int(frame_ms))
        activity_frames = max(1, int(activity_window_seconds * 1000 / self.frame_ms))

        self._lock = threading.RLock()
        self._sum_squares = 0.0
        self._samples = 0
        self._peak = 0
        self._window_started = time.monotonic()
        self._activity: Deque[bool] = deque(maxlen=activity_frames)

        self.level_db: Optional[float] = None
        self.peak_db: Optional[float] = None
        self.channel_levels_db: List[Optional[float]] = [None]
        self.history: Deque[float] = deque(maxlen=self.history_max)
        self.updated_at: Optional[float] = None
        self.updated_mono: Optional[float] = None
        self.frame_count = 0
        self.sample_rate = 16000
        self.channels = 1

    def update(self, frame: AudioFrame) -> None:
        samples = frame.sample_count
        if samples <= 0:
            return
        self.sample_rate = frame.sample_rate
        self.channels = frame.channels
        chunk = frame.pcm[: samples * frame.block_align]
        view = memoryview(chunk).cast("h")

        sum_squares = 0
        peak = 0
        for value in view:
            sum_squares += value * value
            magnitude = -value if value < 0 else value
            if magnitude > peak:
                peak = magnitude
        frame_rms = math.sqrt(sum_squares / samples) if samples else 0.0
        frame_db = to_dbfs(frame_rms)

        now = time.monotonic()
        emit = False
        with self._lock:
            self.frame_count += 1
            self._sum_squares += sum_squares
            self._samples += samples
            if peak > self._peak:
                self._peak = peak
            self._activity.append(frame_db > self.active_threshold)
            if now - self._window_started >= self.update_interval:
                emit = True
        if emit:
            self._emit(now)

    def _emit(self, now: float) -> None:
        with self._lock:
            if self._samples <= 0:
                self._window_started = now
                return
            rms = math.sqrt(self._sum_squares / self._samples)
            peak = self._peak
            self._sum_squares = 0.0
            self._samples = 0
            self._peak = 0
            self._window_started = now
            level_db = round(to_dbfs(rms), 1)
            peak_db = round(to_dbfs(peak), 1)
            self.level_db = level_db
            self.peak_db = peak_db
            self.channel_levels_db = [level_db]
            self.history.append(level_db)
            self.updated_at = time.time()
            self.updated_mono = now

    def silence_ratio(self) -> float:
        with self._lock:
            activity = list(self._activity)
        if not activity:
            return 1.0
        return round(sum(1 for active in activity if not active) / len(activity), 3)

    def activity_ratio(self) -> float:
        return round(1.0 - self.silence_ratio(), 3)

    def is_active(self, stale_after_seconds: float = 1.0) -> bool:
        with self._lock:
            updated = self.updated_mono
        return updated is not None and (time.monotonic() - updated) <= stale_after_seconds

    def snapshot(self, stale_after_seconds: float = 1.0) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            active = self.updated_mono is not None and (now - self.updated_mono) <= stale_after_seconds
            history = list(self.history)
            span = len(history) * self.update_interval
            return {
                "active": active,
                "level_db": self.level_db if active else None,
                "peak_db": self.peak_db if active else None,
                "channel_levels_db": (
                    list(self.channel_levels_db) if active else [None] * self.channels
                ),
                "history": history,
                "history_seconds": round(min(span, len(history) * self.update_interval), 1),
                "updated_at": self.updated_at,
                "age_ms": round((now - self.updated_mono) * 1000.0, 1) if self.updated_mono else None,
                "frame_count": self.frame_count,
                "silence_ratio": self.silence_ratio(),
                "activity_ratio": self.activity_ratio(),
                "active_threshold_dbfs": self.active_threshold,
                "update_hz": round(1.0 / self.update_interval, 1),
            }
