"""AudioProcessor adapter for the long-lived speaker identity service."""
from __future__ import annotations

from typing import Any, Dict

from input.audio_frame import AudioFrame
from processors.base import BaseProcessor


class SpeakerIdentityProcessor(BaseProcessor):
    name = "speaker_identity"

    def __init__(self, service):
        super().__init__()
        self.service = service

    async def accept(self, frame: AudioFrame) -> None:
        await super().accept(frame)
        await self.service.accept(frame)

    def status(self) -> Dict[str, Any]:
        payload = super().status()
        service = self.service.status()
        payload.update({
            "ready": service["ready"],
            "profile_count": service["profile_count"],
            "latency": service["latency"],
        })
        return payload
