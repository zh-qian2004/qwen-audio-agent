from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from input.audio_frame import AudioFormat, make_frame
from speaker_identity import SpeakerIdentityService


class FakeProvider:
    name = "fake"

    def load(self):
        return {"provider": self.name, "dimension": 3, "load_ms": 1.0}

    def embed(self, pcm, sample_rate=16000):
        assert pcm
        return [1.0, 0.0, 0.0]


def config(tmp_path: Path):
    return {
        "enabled": True,
        "registry_path": str(tmp_path / "speakers.json"),
        "sample_rate": 16000,
        "frame_ms": 20,
        "energy_threshold_dbfs": -60,
        "start_frames": 1,
        "end_silence_ms": 20,
        "pre_roll_ms": 20,
        "min_audio_ms": 1000,
    }


def frame(sequence: int, value: int = 2000):
    pcm = int(value).to_bytes(2, "little", signed=True) * 320
    return make_frame(
        sequence=sequence,
        pcm=pcm,
        fmt=AudioFormat(16000, 1, 2),
        captured_at_ns=time.time_ns(),
    )


@pytest.mark.asyncio
async def test_enrollment_persists_profile_without_raw_audio(tmp_path):
    service = SpeakerIdentityService(config(tmp_path), provider=FakeProvider())
    assert await service.start()
    await service.begin_enrollment("owner", "主人", "owner")
    for sequence in range(1, 55):
        await service.accept(frame(sequence))
    result = await service.finish_enrollment()
    assert result["speaker_id"] == "owner"
    assert service.list_profiles()[0]["role"] == "owner"
    payload = json.loads((tmp_path / "speakers.json").read_text("utf-8"))
    assert "pcm" not in json.dumps(payload)
    assert len(payload["items"][0]["embedding"]) == 3
    await service.stop()


@pytest.mark.asyncio
async def test_identity_event_is_emitted_after_speech(tmp_path):
    service = SpeakerIdentityService(config(tmp_path), provider=FakeProvider())
    assert await service.start()
    service.profiles["owner"] = {
        "speaker_id": "owner",
        "display_name": "主人",
        "role": "owner",
        "embedding": [1.0, 0.0, 0.0],
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    queue = service.subscribe()
    for sequence in range(1, 56):
        await service.accept(frame(sequence))
    await service.accept(frame(56, 0))
    events = []
    for _ in range(3):
        events.append(await asyncio.wait_for(queue.get(), timeout=1.0))
        if events[-1]["type"] == "speaker.identity":
            break
    identity = next(item for item in events if item["type"] == "speaker.identity")
    assert identity["status"] == "verified"
    assert identity["speaker_id"] == "owner"
    assert identity["inference_ms"] >= 0
    await service.stop()


@pytest.mark.asyncio
async def test_benchmark_reports_realtime_factor(tmp_path):
    service = SpeakerIdentityService(config(tmp_path), provider=FakeProvider())
    assert await service.start()
    result = await service.benchmark(frame(1).pcm * 50, iterations=3)
    assert result["iterations"] == 3
    assert result["audio_ms"] == 1000
    assert result["realtime_factor_mean"] >= 0
    await service.stop()
