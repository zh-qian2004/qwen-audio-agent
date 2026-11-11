"""测试用 Mock TSE 消费者（目标说话人提取占位，不做真实推理）。

用途：验证它可以和 Mock ASR、Dashboard、WebSocket 等消费者同时消费同一路音频，
且慢速消费者不会影响它的帧率。
"""
from __future__ import annotations

import math
from typing import Any, Dict

from input.audio_frame import AudioFrame
from processors.base import BaseProcessor


class MockTseProcessor(BaseProcessor):
    name = "mock_tse"

    def __init__(self) -> None:
        super().__init__()
        self._sum_squares = 0.0
        self._samples = 0
        self.energy_dbfs = None
        self.session_id = None

    async def start(self, session) -> None:
        await super().start(session)
        self.session_id = getattr(session, "session_id", None)
        self.pre_roll_frames = int(getattr(session, "pre_roll_frames", 0) or 0)

    async def accept(self, frame: AudioFrame) -> None:
        await super().accept(frame)
        samples = frame.sample_count
        if not samples:
            return
        view = memoryview(frame.pcm[: samples * frame.block_align]).cast("h")
        for value in view:
            self._sum_squares += value * value
        self._samples += samples
        if self._samples:
            rms = math.sqrt(self._sum_squares / self._samples)
            self.energy_dbfs = round(20.0 * math.log10(rms / 32768.0), 1) if rms > 0 else -96.0

    def status(self) -> Dict[str, Any]:
        payload = super().status()
        payload.update(
            {
                "session_id": self.session_id,
                "pre_roll_frames": getattr(self, "pre_roll_frames", 0),
                "energy_dbfs": self.energy_dbfs,
                "gap_free": not self.errors,
            }
        )
        return payload
