"""Realtime output conversion, WebSocket protocol and speaker arbitration."""
from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.output_routes import create_output_router
from conftest import FakeConverter, FakeProvider, FakePublisher, base_config, wait_terminal
from playback_arbiter import PlaybackArbiter
from realtime_output import Pcm24kMonoTo48kStereo, RealtimeOutputService
from state import ServiceError
from tts_service import TtsService


def pcm_samples(count: int) -> bytes:
    values = [((index * 997) % 30000) - 15000 for index in range(count)]
    return struct.pack(f"<{count}h", *values)


def start_event(stream_id: str = "stream-1", response_id: str = "response-1") -> dict:
    return {
        "type": "audio_start",
        "stream_id": stream_id,
        "response_id": response_id,
        "encoding": "pcm_24000_16_1",
        "sample_rate": 24000,
        "channels": 1,
        "sample_width": 2,
    }


def make_output_app(config=None):
    config = config or base_config()
    publisher = FakePublisher()
    arbiter = PlaybackArbiter(config["playback"])
    output = RealtimeOutputService(config, publisher, arbiter)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        publisher.start()
        try:
            yield
        finally:
            await output.stop()
            await arbiter.stop()
            publisher.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_output_router(output))
    return app, output, publisher, arbiter


def test_streaming_converter_handles_arbitrary_chunks_and_residual_state():
    source = pcm_samples(5001)
    whole = Pcm24kMonoTo48kStereo()
    expected = whole.feed(source) + whole.finish()

    split = Pcm24kMonoTo48kStereo()
    offsets = [1, 2, 7, 31, 513, 1024, 4097, len(source)]
    actual = bytearray()
    start = 0
    for end in offsets:
        actual.extend(split.feed(source[start:end]))
        start = end
    actual.extend(split.finish())

    assert bytes(actual) == expected
    assert len(actual) == 5001 * 2 * 2 * 2  # samples × 2x rate × stereo × S16
    frames = struct.unpack(f"<{len(actual) // 2}h", actual)
    assert all(frames[index] == frames[index + 1] for index in range(0, len(frames), 2))


def test_streaming_converter_rejects_incomplete_final_sample():
    converter = Pcm24kMonoTo48kStereo()
    converter.feed(b"\x01")
    with pytest.raises(ServiceError) as info:
        converter.finish()
    assert info.value.code == "pcm_frame_incomplete"


def test_websocket_normal_start_pcm_end_and_event_order():
    app, output, publisher, _arbiter = make_output_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as ws:
            ws.send_json(start_event())
            ready = ws.receive_json()
            assert ready["type"] == "output_ready"
            ws.send_bytes(pcm_samples(1200)[:1])
            ws.send_bytes(pcm_samples(1200)[1:337])
            ws.send_bytes(pcm_samples(1200)[337:])
            ws.send_json({"type": "audio_end"})
            started = ws.receive_json()
            ended = ws.receive_json()

    assert [ready["type"], started["type"], ended["type"]] == [
        "output_ready", "playback_started", "playback_ended"
    ]
    assert started["published_packets"] == 1
    assert ended["published_bytes"] == 1200 * 8
    assert sum(publisher.packets) == 1200 * 8
    assert output.status()["phase"] == "idle"


def test_burst_input_applies_backpressure_instead_of_overflow():
    config = base_config(output={
        "buffer_limit_bytes": 7680,
        "chunk_ms": 20,
        "publish_lead_chunks": 0,
        "playback_drain_seconds": 0.0,
    })
    app, output, publisher, _arbiter = make_output_app(config)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as ws:
            ws.send_json(start_event())
            assert ws.receive_json()["type"] == "output_ready"
            ws.send_bytes(pcm_samples(4800))
            ws.send_json({"type": "audio_end"})
            events = []
            while not events or events[-1]["type"] not in ("playback_ended", "output_error"):
                events.append(ws.receive_json())

    assert events[-1]["type"] == "playback_ended"
    assert sum(publisher.packets) == 4800 * 8
    assert output.status()["stats"]["backpressure_waits"] > 0


@pytest.mark.parametrize(
    "payload,code",
    [
        ({**start_event(), "sample_rate": 16000}, "unsupported_audio_format"),
        ({"type": "audio_end"}, "audio_start_required"),
    ],
)
def test_websocket_rejects_invalid_start(payload, code):
    app, _output, _publisher, _arbiter = make_output_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as ws:
            ws.send_json(payload)
            event = ws.receive_json()
            assert event["type"] == "output_error"
            assert event["error"]["code"] == code


def test_websocket_rejects_invalid_stream_event():
    app, _output, _publisher, _arbiter = make_output_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as ws:
            ws.send_json(start_event())
            assert ws.receive_json()["type"] == "output_ready"
            ws.send_json({"type": "unexpected"})
            event = ws.receive_json()
            assert event["type"] == "output_error"
            assert event["error"]["code"] == "invalid_output_event"


def test_audio_cancel_stops_old_stream_and_releases_owner():
    app, output, publisher, _arbiter = make_output_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as ws:
            ws.send_json(start_event())
            assert ws.receive_json()["type"] == "output_ready"
            ws.send_bytes(pcm_samples(5000))
            assert ws.receive_json()["type"] == "playback_started"
            ws.send_json({"type": "audio_cancel", "reason": "playback.clear"})
            event = ws.receive_json()
            assert event["type"] == "playback_cancelled"
            assert event["reason"] == "playback.clear"
            packets_at_cancel = len(publisher.packets)
            time.sleep(0.08)
            assert len(publisher.packets) == packets_at_cancel
    assert output.status()["arbiter"]["idle"] is True


def test_control_endpoint_cancels_backpressured_stream_immediately():
    config = base_config(output={
        "buffer_limit_bytes": 7680,
        "chunk_ms": 20,
        "publish_lead_chunks": 0,
        "playback_drain_seconds": 0.0,
    })
    app, output, _publisher, _arbiter = make_output_app(config)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as ws:
            ws.send_json(start_event("cancel-stream", "cancel-response"))
            assert ws.receive_json()["type"] == "output_ready"
            ws.send_bytes(pcm_samples(4800))
            assert ws.receive_json()["type"] == "playback_started"
            result = client.post("/api/audio/output/cancel", json={
                "stream_id": "cancel-stream",
                "response_id": "cancel-response",
                "reason": "user_interruption",
            })
            assert result.status_code == 200
            assert result.json()["accepted"] is True
            cancelled = ws.receive_json()
            assert cancelled["type"] == "playback_cancelled"
            assert cancelled["reason"] == "user_interruption"

    assert output.status()["phase"] == "idle"
    assert output.status()["arbiter"]["idle"] is True


def test_disconnect_releases_stream_and_second_connection_can_start():
    app, output, _publisher, _arbiter = make_output_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as first:
            first.send_json(start_event("first"))
            assert first.receive_json()["type"] == "output_ready"
        deadline = time.monotonic() + 1.0
        while output.status()["active"] is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        with client.websocket_connect("/ws/audio/output") as second:
            second.send_json(start_event("second"))
            assert second.receive_json()["type"] == "output_ready"
            second.send_json({"type": "audio_cancel"})
            assert second.receive_json()["type"] == "playback_cancelled"


def test_concurrent_realtime_connection_gets_busy():
    app, _output, _publisher, _arbiter = make_output_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as first:
            first.send_json(start_event("first"))
            assert first.receive_json()["type"] == "output_ready"
            with client.websocket_connect("/ws/audio/output") as second:
                second.send_json(start_event("second"))
                event = second.receive_json()
                assert event["type"] == "output_error"
                assert event["error"]["code"] == "output_busy"
            first.send_json({"type": "audio_cancel"})
            assert first.receive_json()["type"] == "playback_cancelled"


def test_idle_timeout_reports_error_and_releases_resource():
    config = base_config(output={"idle_timeout_seconds": 0.05})
    app, output, _publisher, _arbiter = make_output_app(config)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/output") as ws:
            ws.send_json(start_event())
            assert ws.receive_json()["type"] == "output_ready"
            event = ws.receive_json()
            assert event["type"] == "output_error"
            assert event["error"]["code"] == "stream_idle_timeout"
    assert output.status()["arbiter"]["idle"] is True


async def test_realtime_preempts_normal_tts_and_normal_tts_waits_for_realtime():
    config = base_config(queue={"playback_drain_seconds": 0.0})
    publisher = FakePublisher(chunk_delay=0.02)
    arbiter = PlaybackArbiter(config["playback"])
    service = TtsService(
        config,
        provider=FakeProvider(),
        converter=FakeConverter(duration_seconds=1.2),
        publisher=publisher,
        arbiter=arbiter,
    )
    await service.start()
    try:
        first = await service.speak("普通播报")
        for _ in range(100):
            if arbiter.snapshot()["owner"] is not None:
                break
            await asyncio.sleep(0.01)
        realtime = await arbiter.acquire(
            "realtime", "stream", arbiter.priority("realtime"), wait=False
        )
        first_task = await wait_terminal(service, first["task_id"])
        assert first_task["phase"] == "cancelled"
        assert first_task["publish"]["preempted"] is True

        second = await service.speak("等待播放")
        await asyncio.sleep(0.08)
        assert service.get_task(second["task_id"])["phase"] == "publishing"
        assert arbiter.snapshot()["tts_waiting"] == 1
        await arbiter.release(realtime, "test_release")
        second_task = await wait_terminal(service, second["task_id"])
        assert second_task["phase"] == "succeeded"
    finally:
        await service.stop()


async def test_high_priority_tts_preempts_realtime():
    config = base_config(queue={"playback_drain_seconds": 0.0})
    publisher = FakePublisher()
    arbiter = PlaybackArbiter(config["playback"])
    realtime = await arbiter.acquire(
        "realtime", "stream", arbiter.priority("realtime"), wait=False
    )
    service = TtsService(
        config,
        provider=FakeProvider(),
        converter=FakeConverter(duration_seconds=0.12),
        publisher=publisher,
        arbiter=arbiter,
    )
    await service.start()
    try:
        task = await service.speak("高优先级", priority="high")
        await asyncio.wait_for(realtime.cancel_event.wait(), timeout=1.0)
        assert realtime.cancel_reason.startswith("preempted_by:tts:")
        await arbiter.release(realtime, "preempted")
        result = await wait_terminal(service, task["task_id"])
        assert result["phase"] == "succeeded"
    finally:
        await service.stop()
