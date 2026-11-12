"""Speaker enrollment, identity status and event stream routes."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel


class EnrollmentRequest(BaseModel):
    speaker_id: str
    display_name: str = ""
    role: str = "guest"


def create_speaker_router(service) -> APIRouter:
    router = APIRouter()

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, ValueError):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, RuntimeError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    @router.get("/api/speakers/status")
    async def speaker_status():
        return service.status()

    @router.get("/api/speakers")
    async def speaker_list():
        return {"items": service.list_profiles()}

    @router.post("/api/speakers/enroll/start")
    async def enrollment_start(payload: EnrollmentRequest):
        try:
            return await service.begin_enrollment(
                payload.speaker_id, payload.display_name, payload.role
            )
        except Exception as exc:
            raise error(exc) from exc

    @router.post("/api/speakers/enroll/finish")
    async def enrollment_finish():
        try:
            return await service.finish_enrollment()
        except Exception as exc:
            raise error(exc) from exc

    @router.post("/api/speakers/enroll/cancel")
    async def enrollment_cancel():
        return service.cancel_enrollment()

    @router.delete("/api/speakers/{speaker_id}")
    async def speaker_delete(speaker_id: str):
        if not service.delete_profile(speaker_id):
            raise HTTPException(status_code=404, detail="speaker not found")
        return {"deleted": True, "speaker_id": speaker_id}

    @router.websocket("/ws/speakers/events")
    async def speaker_events(websocket: WebSocket):
        await websocket.accept()
        queue = service.subscribe()
        try:
            await websocket.send_json({"type": "speaker.status", **service.status()})
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            service.unsubscribe(queue)

    return router
