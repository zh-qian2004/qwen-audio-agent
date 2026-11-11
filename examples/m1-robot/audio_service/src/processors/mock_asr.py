"""测试用 Mock ASR 消费者（不做真实推理，不注册到生产 API）。

用途：验证 fail_on_gap 会话可以拿到连续帧、pre-roll 生效、会话能正常结束，
并与 Dashboard/WebSocket/TSE 同时消费同一路音频。
"""
from __future__ import annotations

from typing import Any, Dict, List

from input.audio_frame import AudioFrame
from processors.base import BaseProcessor


class MockAsrProcessor(BaseProcessor):
    name = "mock_asr"

    def __init__(self) -> None:
        super().__init__()
        self.transcript = None
        self.gap_free = True
        self.frame_ms_total = 0.0
        self._sequences: List[int] = []

    async def start(self, session) -> None:
        await super().start(session)
        self.pre_roll_frames = int(getattr(session, "pre_roll_frames", 0) or 0)
        self.session_id = getattr(session, "session_id", None)

    async def accept(self, frame: AudioFrame) -> None:
        await super().accept(frame)
        self.frame_ms_total += frame.duration_ms
        if len(self._sequences) < 8:
            self._sequences.append(frame.sequence)

    async def finish(self) -> Dict[str, Any]:
        self.transcript = (
            f"mock-asr: {self.frames} frames / {self.frame_ms_total:.1f} ms"
        )
        return await super().finish()

    def status(self) -> Dict[str, Any]:
        payload = super().status()
        payload.update(
            {
                "session_id": getattr(self, "session_id", None),
                "pre_roll_frames": getattr(self, "pre_roll_frames", 0),
                "duration_ms": round(self.frame_ms_total, 1),
                "transcript": self.transcript,
                "gap_free": not self.errors,
                "first_sequences": list(self._sequences),
            }
        )
        return payload
