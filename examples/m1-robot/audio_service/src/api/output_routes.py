"""Realtime audio output WebSocket and control routes."""
from fastapi import APIRouter, Body, WebSocket


def create_output_router(output_service) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/audio/output")
    async def audio_output_stream(websocket: WebSocket):
        await output_service.handle(websocket)

    @router.post("/api/audio/output/cancel")
    async def cancel_audio_output(payload: dict = Body(default={})):
        return await output_service.cancel(
            stream_id=payload.get("stream_id"),
            response_id=payload.get("response_id"),
            reason=payload.get("reason", "external_cancel"),
        )

    return router
