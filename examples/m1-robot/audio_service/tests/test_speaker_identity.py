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
        "enrollment_sample_ms": 1000,
        "enrollment_min_samples": 2,
        "enrollment_target_ms": 2000,
        "auto_finish_enrollment": False,
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
    for sequence in range(1, 105):
        await service.accept(frame(sequence))
    result = await service.finish_enrollment()
    assert result["speaker_id"] == "owner"
    assert service.list_profiles()[0]["role"] == "owner"
    payload = json.loads((tmp_path / "speakers.json").read_text("utf-8"))
    assert "pcm" not in json.dumps(payload)
    assert len(payload["items"][0]["embedding"]) == 3
    assert payload["items"][0]["sample_count"] == 2
    assert result["sample_count"] == 2
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


@pytest.mark.asyncio
async def test_short_segments_accumulate_evidence_instead_of_being_discarded(tmp_path):
    cfg = config(tmp_path)
    cfg["evidence_target_ms"] = 1000
    service = SpeakerIdentityService(cfg, provider=FakeProvider())
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
    sequence = 0
    for _turn in range(2):
        for _ in range(25):
            sequence += 1
            await service.accept(frame(sequence))
        sequence += 1
        await service.accept(frame(sequence, 0))

    identities = []
    for _ in range(6):
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        if event["type"] == "speaker.identity":
            identities.append(event)
        if len(identities) == 2:
            break
    assert identities[0]["status"] == "insufficient_audio"
    assert identities[0]["reason"] == "too_short"
    assert identities[1]["status"] == "verified"
    assert identities[1]["evidence_turns"] == 2
    assert service.status()["decisions"]["too_short"] == 1
    await service.stop()


@pytest.mark.asyncio
async def test_enrollment_auto_finishes_with_multiple_samples(tmp_path):
    cfg = config(tmp_path)
    cfg["auto_finish_enrollment"] = True
    service = SpeakerIdentityService(cfg, provider=FakeProvider())
    assert await service.start()
    queue = service.subscribe()
    await service.begin_enrollment("owner", "主人", "owner")
    for sequence in range(1, 105):
        await service.accept(frame(sequence))
    enrolled = None
    for _ in range(5):
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        if event["type"] == "speaker.enrolled":
            enrolled = event
            break
    assert enrolled is not None
    assert enrolled["sample_count"] == 2
    assert service.enrollment is None
    assert service.list_profiles()[0]["sample_count"] == 2
    await service.stop()


@pytest.mark.asyncio
async def test_playback_dominated_segment_is_not_identified(tmp_path):
    state = {"value": "idle"}
    service = SpeakerIdentityService(
        config(tmp_path),
        provider=FakeProvider(),
        acoustic_state_provider=lambda: {"last": {"state": state["value"]}},
    )
    assert await service.start()
    service.profiles["owner"] = {
        "speaker_id": "owner", "display_name": "主人", "role": "owner",
        "embedding": [1.0, 0.0, 0.0], "created_at": 1.0, "updated_at": 1.0,
    }
    queue = service.subscribe()
    await service.accept(frame(1))
    state["value"] = "suppressed"
    await service.accept(frame(2))
    identity = None
    for _ in range(3):
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        if event["type"] == "speaker.identity":
            identity = event
            break
    assert identity is not None
    assert identity["status"] == "unknown"
    assert identity["reason"] == "playback_active"
    assert service.status()["decisions"]["playback_active"] == 1
    await service.stop()


@pytest.mark.asyncio
async def test_adaptive_energy_gate_accepts_quiet_voice_above_noise_floor(tmp_path):
    cfg = config(tmp_path)
    cfg.update({
        "energy_threshold_dbfs": -42.0,
        "adaptive_energy_enabled": True,
        "energy_floor_dbfs": -55.0,
        "energy_ceiling_dbfs": -35.0,
        "vad_snr_db": 6.0,
    })
    service = SpeakerIdentityService(cfg, provider=FakeProvider())
    assert await service.start()
    service.profiles["owner"] = {
        "speaker_id": "owner", "display_name": "主人", "role": "owner",
        "embedding": [1.0, 0.0, 0.0], "created_at": 1.0, "updated_at": 1.0,
    }
    assert service.status()["thresholds"]["current_energy_dbfs"] == -49.0
    queue = service.subscribe()
    for sequence in range(1, 56):
        await service.accept(frame(sequence, 150))
    await service.accept(frame(56, 0))
    identity = None
    for _ in range(3):
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        if event["type"] == "speaker.identity":
            identity = event
            break
    assert identity is not None
    assert identity["status"] == "verified"
    await service.stop()
