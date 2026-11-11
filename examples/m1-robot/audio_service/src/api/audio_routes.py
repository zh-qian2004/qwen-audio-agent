"""音频输入 Gateway 的 HTTP / WebSocket API。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

LOG = logging.getLogger(__name__)

WS_STATS_INTERVAL_SECONDS = 1.0


def create_audio_router(gateway, config: Optional[Dict[str, Any]] = None, output_service=None) -> APIRouter:
    config = config or {}
    stream_capacity = max(1, int(config.get("stream_capacity", 50)))
    router = APIRouter()

    @router.get("/api/audio/status")
    async def audio_status():
        payload = gateway.status()
        if output_service is not None:
            output = output_service.status()
            payload["output"] = output
            payload["playback"] = output["arbiter"]
            payload["speaker"] = output["ros"]
        return payload

    @router.get("/api/audio/level")
    async def audio_level():
        return gateway.level_payload()

    @router.get("/api/audio/sessions")
    async def audio_sessions():
        return {
            "items": gateway.sessions.snapshot(),
            "available_types": gateway.sessions.available_types,
        }

    @router.websocket("/ws/audio/input")
    async def audio_input_stream(websocket: WebSocket):
        params = websocket.query_params
        try:
            pre_roll_ms = int(params.get("pre_roll_ms", 0) or 0)
        except ValueError:
            await websocket.close(code=1008, reason="invalid pre_roll_ms")
            return
        if pre_roll_ms < 0 or pre_roll_ms > gateway.pre_roll_max_ms:
            await websocket.close(
                code=1008, reason=f"pre_roll_ms 需在 0..{gateway.pre_roll_max_ms}"
            )
            return
        delivery = str(params.get("delivery", "latest") or "latest").lower()
        if delivery not in ("latest", "continuous"):
            await websocket.close(code=1008, reason="delivery 只支持 latest|continuous")
            return

        policy = "latest" if delivery == "latest" else "drop_oldest"
        capacity = 1 if delivery == "latest" else stream_capacity
        stream_id = uuid.uuid4().hex
        await websocket.accept()

        subscription = gateway.subscribe(
            f"ws-input-{stream_id[:8]}", policy=policy, capacity=capacity
        )
        seeded = 0
        if pre_roll_ms > 0:
            seeded = subscription.seed(gateway.ring.recent(pre_roll_ms))

        sent_frames = 0
        last_sent_sequence = None
        try:
            await websocket.send_json(
                {
                    "type": "audio_start",
                    "stream_id": stream_id,
                    "topic": gateway.topic,
                    "encoding": gateway.encoding,
                    "sample_rate": gateway.sample_rate,
                    "sample_width": gateway.sample_width,
                    "channels": gateway.channels,
                    "frame_ms": gateway.frame_ms,
                    "frame_bytes": gateway.rechunker.frame_bytes,
                    "delivery": delivery,
                    "pre_roll_ms": pre_roll_ms,
                    "pre_roll_frames": seeded,
                    "online": gateway.capture.online(),
                    "ts": round(time.time(), 3),
                }
            )
            stats_at = time.monotonic() + WS_STATS_INTERVAL_SECONDS
            while True:
                timeout = max(0.05, stats_at - time.monotonic())
                try:
                    frame = await asyncio.wait_for(subscription.__anext__(), timeout=timeout)
                except asyncio.TimeoutError:
                    if time.monotonic() >= stats_at:
                        await websocket.send_json(_stream_stats(subscription, sent_frames, last_sent_sequence))
                        stats_at = time.monotonic() + WS_STATS_INTERVAL_SECONDS
                    continue
                await websocket.send_bytes(frame.pcm)
                sent_frames += 1
                last_sent_sequence = frame.sequence
                if time.monotonic() >= stats_at:
                    await websocket.send_json(_stream_stats(subscription, sent_frames, last_sent_sequence))
                    stats_at = time.monotonic() + WS_STATS_INTERVAL_SECONDS
        except asyncio.CancelledError:
            raise
        except (WebSocketDisconnect, RuntimeError):
            return
        except Exception:  # pragma: no cover - 网络层异常
            LOG.debug("WebSocket 音频流异常", exc_info=True)
        finally:
            subscription.close()
            LOG.info(
                "WebSocket 音频流关闭 stream=%s delivery=%s sent=%d dropped=%d",
                stream_id,
                delivery,
                sent_frames,
                subscription.metrics["dropped_frames"],
            )

    return router


def _stream_stats(subscription, sent_frames: int, last_sequence: Optional[int]) -> Dict[str, Any]:
    stats = subscription.stats()
    sent_at_ms = time.time_ns() // 1_000_000
    return {
        "type": "audio_stats",
        "sent_frames": sent_frames,
        "dropped_frames": stats["dropped_frames"],
        "queue_depth": stats["queue_depth"],
        "last_sequence": last_sequence,
        "lag_ms": stats["lag_ms"],
        "capture_to_robot_send_ms": stats["lag_ms"],
        "server_sent_at_ms": sent_at_ms,
        "ts": sent_at_ms / 1000.0,
    }
