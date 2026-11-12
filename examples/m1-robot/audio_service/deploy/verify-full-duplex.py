"""Stress realtime output with silence while verifying microphone continuity."""
from __future__ import annotations

import asyncio
import json
import time

import websockets


INPUT_BASE = "ws://127.0.0.1:8093"
OUTPUT_BASE = "ws://127.0.0.1:8091"
SECONDS = 12


async def main() -> None:
    input_stats = []
    input_frames = 0
    input_started = asyncio.Event()
    stop_input = asyncio.Event()

    async with websockets.connect(
        f"{INPUT_BASE}/ws/audio/input?delivery=continuous&pre_roll_ms=0",
        max_size=None,
    ) as input_ws, websockets.connect(
        f"{OUTPUT_BASE}/ws/audio/output", max_size=None
    ) as output_ws:

        async def receive_input() -> None:
            nonlocal input_frames
            while not stop_input.is_set():
                try:
                    message = await asyncio.wait_for(input_ws.recv(), timeout=1.5)
                except asyncio.TimeoutError:
                    continue
                if isinstance(message, bytes):
                    input_frames += 1
                    continue
                event = json.loads(message)
                if event.get("type") == "audio_start":
                    input_started.set()
                elif event.get("type") == "audio_stats":
                    input_stats.append(event)

        input_task = asyncio.create_task(receive_input())
        await asyncio.wait_for(input_started.wait(), timeout=3)

        await output_ws.send(json.dumps({
            "type": "audio_start",
            "stream_id": "full-duplex-verification",
            "response_id": "full-duplex-verification",
            "encoding": "pcm_24000_16_1",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
        }))
        ready = json.loads(await asyncio.wait_for(output_ws.recv(), timeout=5))
        assert ready["type"] == "output_ready", ready

        started_at = time.monotonic()
        # Feed 500 ms chunks faster than realtime. Server-side backpressure then
        # exercises conversion and playback while input must remain responsive.
        for _ in range(SECONDS * 2):
            await output_ws.send(bytes(24_000))
            await asyncio.sleep(0.05)
        await output_ws.send(json.dumps({"type": "audio_end"}))

        output_events = [ready]
        while output_events[-1].get("type") != "playback_ended":
            event = json.loads(await asyncio.wait_for(output_ws.recv(), timeout=20))
            output_events.append(event)
            assert event.get("type") != "output_error", event
        elapsed = time.monotonic() - started_at
        await asyncio.sleep(1.1)
        stop_input.set()
        await input_task

    assert input_stats, "input stream emitted no audio_stats"
    latest = input_stats[-1]
    assert input_frames >= SECONDS * 40, (input_frames, latest)
    assert latest["dropped_frames"] <= 5, latest
    assert latest["queue_depth"] <= 10, latest
    assert latest["capture_to_robot_send_ms"] <= 300, latest
    print(json.dumps({
        "passed": True,
        "output_seconds": SECONDS,
        "wall_seconds": round(elapsed, 3),
        "input_frames": input_frames,
        "input_stats": latest,
        "output_events": [event["type"] for event in output_events],
    }, ensure_ascii=False))


asyncio.run(main())
