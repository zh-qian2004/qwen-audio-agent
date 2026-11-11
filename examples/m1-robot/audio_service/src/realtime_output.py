"""Realtime PCM WebSocket playback service."""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque
from typing import Any, Dict, Optional

from fastapi import WebSocket, WebSocketDisconnect

from playback_arbiter import PRIORITY_REALTIME, PlaybackArbiter, PlaybackLease
from state import ServiceError, redact_secret

LOG = logging.getLogger(__name__)


class Pcm24kMonoTo48kStereo:
    """Incremental linear 2x resampler that is invariant to WebSocket chunking."""

    def __init__(self):
        self._byte_residual = b""
        self._previous: Optional[int] = None
        self.input_bytes = 0
        self.output_bytes = 0

    @property
    def buffered_input_bytes(self) -> int:
        return len(self._byte_residual) + (2 if self._previous is not None else 0)

    def feed(self, payload: bytes) -> bytes:
        self.input_bytes += len(payload)
        data = self._byte_residual + bytes(payload)
        aligned = len(data) & ~1
        self._byte_residual = data[aligned:]
        if not aligned:
            return b""
        samples = struct.unpack(f"<{aligned // 2}h", data[:aligned])
        output = bytearray()
        for sample in samples:
            if self._previous is None:
                self._previous = sample
                continue
            midpoint = (self._previous + sample) // 2
            output.extend(struct.pack("<hhhh", self._previous, self._previous, midpoint, midpoint))
            self._previous = sample
        result = bytes(output)
        self.output_bytes += len(result)
        return result

    def finish(self) -> bytes:
        if self._byte_residual:
            raise ServiceError(
                "pcm_frame_incomplete",
                "PCM 流以不完整的 16-bit sample 结束",
                400,
                {"residual_bytes": len(self._byte_residual)},
            )
        if self._previous is None:
            return b""
        result = struct.pack("<hhhh", self._previous, self._previous, self._previous, self._previous)
        self._previous = None
        self.output_bytes += len(result)
        return result


class RealtimeOutputService:
    VERSION = "1.3.0"

    def __init__(self, config: Dict[str, Any], publisher: Any, arbiter: PlaybackArbiter):
        self.config = config or {}
        output = self.config.get("output", {})
        queue = self.config.get("queue", {})
        self.publisher = publisher
        self.arbiter = arbiter
        self.idle_timeout = max(0.1, float(output.get("idle_timeout_seconds", 5.0)))
        self.max_stream_seconds = max(0.1, float(output.get("max_stream_seconds", 300.0)))
        self.buffer_limit_bytes = max(7680, int(output.get("buffer_limit_bytes", 384000)))
        self.chunk_ms = max(5.0, float(output.get("chunk_ms", 20.0)))
        self.chunk_bytes = int(48000 * self.chunk_ms / 1000.0) * 4
        self.lead_chunks = max(0, int(output.get("publish_lead_chunks", 2)))
        self.start_timeout = max(0.1, float(output.get("start_timeout_seconds", 5.0)))
        self.drain_seconds = max(0.0, float(output.get("playback_drain_seconds", queue.get("playback_drain_seconds", 0.4))))
        self._active: Optional[Dict[str, Any]] = None
        self._active_lock = asyncio.Lock()
        self._stopping = False
        self._recent: deque = deque(maxlen=max(5, int(output.get("recent_events", 20))))
        self.counters = {
            "started": 0,
            "completed": 0,
            "cancelled": 0,
            "failed": 0,
            "received_packets": 0,
            "received_bytes": 0,
            "converted_bytes": 0,
            "published_packets": 0,
            "published_bytes": 0,
            "backpressure_waits": 0,
        }

    async def stop(self) -> None:
        self._stopping = True
        active = self._active
        if active is not None:
            active["cancel"].set()
            async with active["buffer_condition"]:
                active["buffer_condition"].notify_all()

    async def cancel(
        self,
        *,
        stream_id: Optional[str] = None,
        response_id: Optional[str] = None,
        reason: str = "external_cancel",
    ) -> Dict[str, Any]:
        """Cancel the active stream without waiting behind buffered PCM frames."""
        requested_stream = str(stream_id or "").strip()
        requested_response = str(response_id or "").strip()
        cancel_reason = str(reason or "external_cancel").strip()[:128] or "external_cancel"
        async with self._active_lock:
            active = self._active
            if active is None:
                return {"ok": True, "accepted": False, "reason": "no_active_stream"}
            if requested_stream and requested_stream != active["stream_id"]:
                return {"ok": True, "accepted": False, "reason": "stream_mismatch"}
            if requested_response and requested_response != active["response_id"]:
                return {"ok": True, "accepted": False, "reason": "response_mismatch"}
            active["cancel_reason"] = cancel_reason
            active["cancel"].set()
            condition: asyncio.Condition = active["buffer_condition"]

        async with condition:
            condition.notify_all()
        LOG.info(
            "实时输出收到独立取消 stream=%s response=%s reason=%s",
            active["stream_id"], active["response_id"], cancel_reason,
        )
        return {
            "ok": True,
            "accepted": True,
            "stream_id": active["stream_id"],
            "response_id": active["response_id"],
            "reason": cancel_reason,
        }

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        lease: Optional[PlaybackLease] = None
        active: Optional[Dict[str, Any]] = None
        try:
            start_message = await asyncio.wait_for(websocket.receive(), timeout=self.start_timeout)
            metadata = self._parse_start(start_message)
            async with self._active_lock:
                if self._active is not None:
                    raise ServiceError(
                        "output_busy",
                        "已有实时音频流正在播放",
                        409,
                        {"stream_id": self._active["stream_id"]},
                    )
                cancel = asyncio.Event()
                active = self._new_active(metadata, cancel)
                self._active = active

            lease = await self.arbiter.acquire(
                "realtime",
                metadata["stream_id"],
                self.arbiter.priority("realtime"),
                {"stream_id": metadata["stream_id"], "response_id": metadata.get("response_id")},
                cancel=cancel,
                wait=False,
            )
            active["phase"] = "ready"
            active["lease"] = lease
            lease.metadata["phase"] = "ready"
            self._remember("ready", active)
            await self._send_event(websocket, "output_ready", active)
            LOG.info("实时输出已就绪 stream=%s response=%s", active["stream_id"], active["response_id"])
            await self._run_stream(websocket, active, lease)
        except WebSocketDisconnect:
            if active is not None:
                await self._cancel_active(active, "websocket_disconnected", send=None)
        except asyncio.TimeoutError:
            await self._safe_error(websocket, ServiceError("audio_start_timeout", "等待 audio_start 超时", 408))
        except ServiceError as exc:
            if active is not None:
                active["error"] = exc.as_dict()["error"]
                active["phase"] = "error"
                self.counters["failed"] += 1
                self._remember("error", active)
            await self._safe_error(websocket, exc, active)
        except Exception as exc:  # pragma: no cover - defensive network boundary
            LOG.exception("实时输出异常")
            error = ServiceError("output_internal_error", redact_secret(exc), 500)
            if active is not None:
                active["error"] = error.as_dict()["error"]
                active["phase"] = "error"
                self.counters["failed"] += 1
                self._remember("error", active)
            await self._safe_error(websocket, error, active)
        finally:
            if active is not None:
                active["cancel"].set()
            await self.arbiter.release(lease, "stream_released")
            async with self._active_lock:
                if self._active is active:
                    self._active = None

    async def _run_stream(self, websocket: WebSocket, active: Dict[str, Any], lease: PlaybackLease) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        receiver = asyncio.create_task(self._receive_loop(websocket, active, queue), name=f"output-rx-{active['stream_id']}")
        player = asyncio.create_task(self._play_loop(websocket, active, queue, lease), name=f"output-play-{active['stream_id']}")
        preempt = asyncio.create_task(lease.cancel_event.wait(), name=f"output-preempt-{active['stream_id']}")
        done, _ = await asyncio.wait((receiver, player, preempt), return_when=asyncio.FIRST_COMPLETED)

        if preempt in done and lease.cancel_event.is_set():
            active["cancel"].set()
            receiver.cancel()
            player.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            await asyncio.gather(player, return_exceptions=True)
            await self._cancel_active(active, lease.cancel_reason or "preempted", send=websocket)
            return

        if receiver in done:
            try:
                receiver_result = receiver.result()
            except WebSocketDisconnect:
                receiver_result = ("disconnected", "websocket_disconnected")
            except Exception:
                active["cancel"].set()
                player.cancel()
                await asyncio.gather(player, return_exceptions=True)
                preempt.cancel()
                await asyncio.gather(preempt, return_exceptions=True)
                raise
            if receiver_result[0] != "ended":
                active["cancel"].set()
                player.cancel()
                await asyncio.gather(player, return_exceptions=True)
                if receiver_result[0] == "disconnected":
                    await self._cancel_active(active, receiver_result[1], send=None)
                else:
                    await self._cancel_active(active, receiver_result[1], send=websocket)
                preempt.cancel()
                await asyncio.gather(preempt, return_exceptions=True)
                return
            drain_done, _ = await asyncio.wait((player, preempt), return_when=asyncio.FIRST_COMPLETED)
            if preempt in drain_done and lease.cancel_event.is_set():
                active["cancel"].set()
                player.cancel()
                await asyncio.gather(player, return_exceptions=True)
                await self._cancel_active(active, lease.cancel_reason or "preempted", send=websocket)
                return
            await player
            if lease.cancel_event.is_set() and active["phase"] != "ended":
                await self._cancel_active(active, lease.cancel_reason or "preempted", send=websocket)
                return
            preempt.cancel()
            await asyncio.gather(preempt, return_exceptions=True)
            return

        if player in done:
            try:
                await player
            except Exception:
                active["cancel"].set()
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                preempt.cancel()
                await asyncio.gather(preempt, return_exceptions=True)
                raise
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            preempt.cancel()
            await asyncio.gather(preempt, return_exceptions=True)
            if active["cancel"].is_set() and active["phase"] not in ("cancelled", "ended"):
                await self._cancel_active(
                    active,
                    active.get("cancel_reason") or "cancelled",
                    send=websocket,
                )

    async def _receive_loop(self, websocket: WebSocket, active: Dict[str, Any], queue: asyncio.Queue):
        converter = Pcm24kMonoTo48kStereo()
        pending = bytearray()
        started = time.monotonic()
        while True:
            if active["cancel"].is_set():
                return ("cancelled", active.get("cancel_reason") or "cancelled")
            if time.monotonic() - started > self.max_stream_seconds:
                raise ServiceError(
                    "stream_duration_exceeded",
                    "实时音频流超过最大时长",
                    400,
                    {"max_stream_seconds": self.max_stream_seconds},
                )
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=self.idle_timeout)
            except asyncio.TimeoutError:
                raise ServiceError(
                    "stream_idle_timeout",
                    "实时音频流空闲超时",
                    408,
                    {"idle_timeout_seconds": self.idle_timeout},
                ) from None
            kind = message.get("type")
            if kind == "websocket.disconnect":
                return ("disconnected", "websocket_disconnected")
            if message.get("bytes") is not None:
                payload = message["bytes"]
                active["received_packets"] += 1
                active["received_bytes"] += len(payload)
                self.counters["received_packets"] += 1
                self.counters["received_bytes"] += len(payload)
                converted = converter.feed(payload)
                active["converted_bytes"] += len(converted)
                self.counters["converted_bytes"] += len(converted)
                pending.extend(converted)
                while len(pending) >= self.chunk_bytes:
                    chunk = bytes(pending[: self.chunk_bytes])
                    del pending[: self.chunk_bytes]
                    await self._queue_chunk(queue, active, chunk)
                active["converter_buffered_bytes"] = converter.buffered_input_bytes
                active["pending_conversion_bytes"] = len(pending)
                continue
            if message.get("text") is None:
                raise ServiceError("invalid_output_event", "WebSocket 消息类型不受支持", 400)
            event = self._parse_json(message["text"])
            event_type = event.get("type")
            if event_type == "audio_end":
                converted = converter.finish()
                active["converted_bytes"] += len(converted)
                self.counters["converted_bytes"] += len(converted)
                pending.extend(converted)
                if pending:
                    await self._queue_chunk(queue, active, bytes(pending))
                    pending.clear()
                active["converter_buffered_bytes"] = 0
                active["pending_conversion_bytes"] = 0
                await queue.put(None)
                return ("ended", None)
            if event_type == "audio_cancel":
                return ("cancelled", str(event.get("reason") or "audio_cancel"))
            raise ServiceError(
                "invalid_output_event",
                "音频流中只允许二进制 PCM、audio_end 或 audio_cancel",
                400,
                {"type": event_type},
            )

    async def _queue_chunk(self, queue: asyncio.Queue, active: Dict[str, Any], chunk: bytes) -> None:
        if len(chunk) > self.buffer_limit_bytes:
            raise ServiceError(
                "output_chunk_too_large",
                "单个实时音频分片超过缓冲区上限",
                400,
                {"buffer_limit_bytes": self.buffer_limit_bytes, "chunk_bytes": len(chunk)},
            )

        condition: asyncio.Condition = active["buffer_condition"]
        waited = False
        async with condition:
            while active["buffered_bytes"] + len(chunk) > self.buffer_limit_bytes:
                if active["cancel"].is_set():
                    return
                if not waited:
                    waited = True
                    active["backpressure_waits"] += 1
                    self.counters["backpressure_waits"] += 1
                await condition.wait()
            if active["cancel"].is_set():
                return
            projected = active["buffered_bytes"] + len(chunk)
            active["buffered_bytes"] = projected
            active["buffer_peak_bytes"] = max(active["buffer_peak_bytes"], projected)
        await queue.put(chunk)

    async def _play_loop(self, websocket: WebSocket, active: Dict[str, Any], queue: asyncio.Queue, lease: PlaybackLease) -> None:
        packet_index = 0
        media_start: Optional[float] = None
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            async with active["buffer_condition"]:
                active["buffered_bytes"] = max(0, active["buffered_bytes"] - len(chunk))
                active["buffer_condition"].notify_all()
            if active["cancel"].is_set() or lease.cancel_event.is_set():
                return
            duration = len(chunk) / (48000 * 4)
            if media_start is None:
                media_start = time.monotonic()
            target = media_start + max(0, packet_index - self.lead_chunks) * (self.chunk_ms / 1000.0)
            if not await self._sleep_until(target, active["cancel"], lease.cancel_event):
                return
            if active["cancel"].is_set() or lease.cancel_event.is_set():
                return
            self.publisher.publish_chunk(chunk)
            active["published_packets"] += 1
            active["published_bytes"] += len(chunk)
            active["published_audio_seconds"] += duration
            self.counters["published_packets"] += 1
            self.counters["published_bytes"] += len(chunk)
            packet_index += 1
            if active["phase"] != "playing":
                active["phase"] = "playing"
                lease.metadata["phase"] = "playing"
                active["playback_started_at"] = time.time()
                self.counters["started"] += 1
                self._remember("started", active)
                await self._send_event(websocket, "playback_started", active)

        if active["cancel"].is_set() or lease.cancel_event.is_set():
            return
        active["phase"] = "draining"
        active["drain_seconds"] = self.drain_seconds
        lease.metadata["phase"] = "draining"
        lease.metadata["drain_seconds"] = self.drain_seconds
        if not await self._sleep_cancelable(self.drain_seconds, active["cancel"], lease.cancel_event):
            return
        active["phase"] = "ended"
        active["playback_ended_at"] = time.time()
        self.counters["completed"] += 1
        self._remember("ended", active)
        await self._send_event(websocket, "playback_ended", active)
        LOG.info(
            "实时输出完成 stream=%s recv=%d converted=%d published=%d packets=%d",
            active["stream_id"], active["received_bytes"], active["converted_bytes"],
            active["published_bytes"], active["published_packets"],
        )

    async def _cancel_active(self, active: Dict[str, Any], reason: str, send: Optional[WebSocket]) -> None:
        if active["phase"] in ("cancelled", "ended"):
            return
        active["cancel"].set()
        active["phase"] = "cancelled"
        active["cancel_reason"] = reason
        active["cancelled_at"] = time.time()
        active["buffered_bytes"] = 0
        self.counters["cancelled"] += 1
        self._remember("cancelled", active)
        if send is not None:
            await self._send_event(send, "playback_cancelled", active, {"reason": reason})
        LOG.info("实时输出取消 stream=%s reason=%s", active["stream_id"], reason)

    def status(self) -> Dict[str, Any]:
        active = self._public_active(self._active) if self._active is not None else None
        return {
            "active": active,
            "stream_id": active.get("stream_id") if active else None,
            "response_id": active.get("response_id") if active else None,
            "phase": active.get("phase") if active else "idle",
            "format": {
                "input_encoding": "pcm_24000_16_1",
                "output_encoding": "pcm_48000_16_2",
                "output_topic": getattr(self.publisher, "speaker_topic", "/audio/speaker_pcm"),
                "chunk_ms": self.chunk_ms,
                "chunk_bytes": self.chunk_bytes,
            },
            "limits": {
                "idle_timeout_seconds": self.idle_timeout,
                "max_stream_seconds": self.max_stream_seconds,
                "buffer_limit_bytes": self.buffer_limit_bytes,
                "start_timeout_seconds": self.start_timeout,
            },
            "stats": dict(self.counters),
            "recent_events": list(self._recent),
            "arbiter": self.arbiter.snapshot(),
            "ros": self.publisher.status(),
        }

    def _parse_start(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect()
        if message.get("text") is None or message.get("bytes") is not None:
            raise ServiceError("audio_start_required", "首条消息必须是 audio_start JSON", 400)
        payload = self._parse_json(message["text"])
        if payload.get("type") != "audio_start":
            raise ServiceError("audio_start_required", "首条消息必须是 audio_start", 400)
        stream_id = str(payload.get("stream_id") or "").strip()
        if not stream_id or len(stream_id) > 128:
            raise ServiceError("invalid_stream_id", "stream_id 不能为空且不能超过 128 字符", 400)
        expected = {
            "encoding": "pcm_24000_16_1",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
        }
        mismatches = {key: {"expected": value, "actual": payload.get(key)} for key, value in expected.items() if payload.get(key) != value}
        if mismatches:
            raise ServiceError("unsupported_audio_format", "实时输出只支持 pcm_24000_16_1", 400, mismatches)
        response_id = str(payload.get("response_id") or "").strip()
        if not response_id or len(response_id) > 256:
            raise ServiceError("invalid_response_id", "response_id 不能为空且不能超过 256 字符", 400)
        return {**expected, "stream_id": stream_id, "response_id": response_id}

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        import json
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            raise ServiceError("invalid_output_event", "WebSocket 文本消息必须是 JSON object", 400) from None
        if not isinstance(value, dict):
            raise ServiceError("invalid_output_event", "WebSocket JSON 必须是 object", 400)
        return value

    def _new_active(self, metadata: Dict[str, Any], cancel: asyncio.Event) -> Dict[str, Any]:
        return {
            **metadata,
            "phase": "acquiring",
            "created_at": time.time(),
            "cancel": cancel,
            "lease": None,
            "received_packets": 0,
            "received_bytes": 0,
            "converted_bytes": 0,
            "published_packets": 0,
            "published_bytes": 0,
            "published_audio_seconds": 0.0,
            "buffered_bytes": 0,
            "buffer_peak_bytes": 0,
            "backpressure_waits": 0,
            "buffer_condition": asyncio.Condition(),
            "converter_buffered_bytes": 0,
            "pending_conversion_bytes": 0,
            "drain_seconds": 0.0,
            "error": None,
            "cancel_reason": None,
        }

    async def _send_event(self, websocket: WebSocket, event_type: str, active: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {
            "type": event_type,
            "stream_id": active.get("stream_id"),
            "response_id": active.get("response_id"),
            "phase": active.get("phase"),
            "received_bytes": active.get("received_bytes", 0),
            "converted_bytes": active.get("converted_bytes", 0),
            "published_bytes": active.get("published_bytes", 0),
            "published_packets": active.get("published_packets", 0),
            "buffered_bytes": active.get("buffered_bytes", 0),
            "ts": round(time.time(), 3),
        }
        if extra:
            payload.update(extra)
        await websocket.send_json(payload)

    async def _safe_error(self, websocket: WebSocket, exc: ServiceError, active: Optional[Dict[str, Any]] = None) -> None:
        try:
            payload = exc.as_dict()["error"]
            await websocket.send_json({
                "type": "output_error",
                "stream_id": active.get("stream_id") if active else None,
                "response_id": active.get("response_id") if active else None,
                "error": payload,
                "ts": round(time.time(), 3),
            })
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def _sleep_until(self, target: float, *events: asyncio.Event) -> bool:
        return await self._sleep_cancelable(max(0.0, target - time.monotonic()), *events)

    @staticmethod
    async def _sleep_cancelable(seconds: float, *events: asyncio.Event) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if any(event.is_set() for event in events):
                return False
            await asyncio.sleep(min(0.01, deadline - time.monotonic()))
        return not any(event.is_set() for event in events)

    def _remember(self, event: str, active: Dict[str, Any]) -> None:
        self._recent.appendleft({
            "event": event,
            "at": round(time.time(), 3),
            "stream_id": active.get("stream_id"),
            "response_id": active.get("response_id"),
            "phase": active.get("phase"),
            "received_bytes": active.get("received_bytes", 0),
            "converted_bytes": active.get("converted_bytes", 0),
            "published_bytes": active.get("published_bytes", 0),
            "published_packets": active.get("published_packets", 0),
            "reason": active.get("cancel_reason"),
            "error": active.get("error"),
        })

    @staticmethod
    def _public_active(active: Dict[str, Any]) -> Dict[str, Any]:
        hidden = {"cancel", "lease", "buffer_condition"}
        result = {key: value for key, value in active.items() if key not in hidden}
        for key, value in list(result.items()):
            if isinstance(value, float):
                result[key] = round(value, 3)
        return result
