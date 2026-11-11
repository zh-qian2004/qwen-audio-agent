"""WebSocket API for Audio Agent vendor-event decisions."""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


def create_vendor_event_router(bridge) -> APIRouter:
    router = APIRouter()

    @router.get("/api/vendor/events/status")
    async def vendor_event_status():
        return bridge.status()

    @router.websocket("/ws/vendor/events")
    async def vendor_event_stream(websocket: WebSocket):
        await websocket.accept()
        session = None
        sender = None
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
            if hello.get("type") != "agent_hello":
                await websocket.close(code=1008, reason="agent_hello required")
                return
            session = bridge.attach_agent(str(hello.get("instance_id") or "audio-agent"))
            bridge.heartbeat(session.token, ready=hello.get("ready", True) is True)
            await websocket.send_json({
                "type": "vendor_event_ready",
                "instance_id": session.instance_id,
                "decision_timeout_seconds": bridge.decision_timeout_seconds,
            })

            async def send_events():
                while True:
                    event = await session.queue.get()
                    await websocket.send_json(event)

            sender = asyncio.create_task(send_events(), name="vendor-event-ws-sender")
            while True:
                message = await websocket.receive_json()
                message_type = message.get("type")
                if message_type == "agent_heartbeat":
                    bridge.heartbeat(session.token, ready=message.get("ready", True) is True)
                    await websocket.send_json({"type": "agent_heartbeat_ack"})
                elif message_type == "vendor_event_decision":
                    accepted = bridge.decide(
                        session.token,
                        str(message.get("event_id") or ""),
                        str(message.get("action") or ""),
                        str(message.get("reason") or ""),
                    )
                    await websocket.send_json({
                        "type": "vendor_event_decision_ack",
                        "event_id": message.get("event_id"),
                        "accepted": accepted,
                    })
        except asyncio.TimeoutError:
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=1008, reason="agent_hello timeout")
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if sender is not None:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender
            if session is not None:
                bridge.detach_agent(session.token)

    return router
