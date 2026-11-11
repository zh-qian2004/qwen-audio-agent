"""测试公共 fixture：全部使用 mock Provider / mock ROS publisher。

测试期间不产生真实语音、不访问云端、不接触 ALSA 设备。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from audio_converter import PcmAudio  # noqa: E402
from state import ServiceError  # noqa: E402
from tts_service import TtsService  # noqa: E402


def base_config(**overrides) -> dict:
    config = {
        "tts": {
            "model": "qwen-audio-3.1-tts-flash",
            "voice": "longanhuan_v3.1",
            "voices": [
                {"id": "longanhuan_v3.1", "label": "龙安欢"},
                {"id": "xunanchuan_v3.1", "label": "许南川"},
            ],
            "emotions": [
                {"id": "neutral", "label": "自然"},
                {"id": "excited", "label": "兴奋"},
            ],
            "default_emotion": "neutral",
            "speech_rate": {"default": 1.0, "min": 0.5, "max": 2.0, "step": 0.05},
            "base_websocket_api_url": "wss://example.invalid/api-ws/v1/inference",
            "api_key_env": "DASHSCOPE_API_KEY",
            "max_text_length": 20,
            "synthesis_timeout_seconds": 1.0,
            "decode_timeout_seconds": 1.0,
            "idempotency_ttl_seconds": 60,
        },
        "queue": {"capacity": 3, "publish_timeout_seconds": 5.0, "playback_drain_seconds": 0.01},
        "playback": {
            "priorities": {"tts_normal": 10, "realtime": 20, "tts_high": 30},
            "acquire_timeout_seconds": 2.0,
        },
        "output": {
            "start_timeout_seconds": 0.5,
            "idle_timeout_seconds": 0.5,
            "max_stream_seconds": 5.0,
            "buffer_limit_bytes": 384000,
            "chunk_ms": 20,
            "publish_lead_chunks": 2,
            "playback_drain_seconds": 0.0,
        },
        "audio": {
            "sample_rate": 48000,
            "channels": 2,
            "sample_width": 2,
            "chunk_ms": 60,
            "publish_lead_chunks": 2,
            "ffmpeg_path": "ffmpeg",
            "ffprobe_path": "ffprobe",
            "decode_timeout_seconds": 1.0,
        },
        "ros": {
            "node_name": "test_publisher",
            "speaker_topic": "/audio/speaker_pcm",
            "speaker_status_topic": "/audio/speaker_pcm_status",
            "capture_topic": "/audio/capture_pcm",
            "encoding": "pcm_48000_16_2",
            "qos_depth": 10,
        },
        "media_audio": {"process_name": "media_audio"},
        "history": {"max_items": 20},
    }
    for section, values in overrides.items():
        config.setdefault(section, {}).update(values)
    return config


class FakeProvider:
    def __init__(self, mp3: bytes = b"ID3fake-mp3", error: Exception | None = None, delay: float = 0.0):
        self.mp3 = mp3
        self.error = error
        self.delay = delay
        self.calls = []
        self.status_calls = 0

    def synthesize(self, text: str, voice=None, emotion="neutral", speech_rate=1.0) -> dict:
        self.calls.append({
            "text": text,
            "voice": voice,
            "emotion": emotion,
            "speech_rate": speech_rate,
        })
        if self.delay:
            import time

            time.sleep(self.delay)
        if self.error:
            raise self.error
        return {
            "data": self.mp3,
            "bytes": len(self.mp3),
            "model": "qwen-audio-3.1-tts-flash",
            "voice": voice or "longanhuan_v3.1",
            "emotion": emotion,
            "speech_rate": speech_rate,
            "latency_ms": 12.5,
        }

    def status(self) -> dict:
        self.status_calls += 1
        return {
            "available": self.error is None,
            "unavailable_reason": None,
            "model": "qwen-audio-3.1-tts-flash",
            "voice": "longanhuan_v3.1",
            "endpoint": "wss://example.invalid",
            "api_key_env": "DASHSCOPE_API_KEY",
            "api_key_configured": True,
            "api_key_masked": "sk-t****",
            "requests": len(self.calls),
            "successes": len(self.calls),
            "failures": 0,
            "last_success_at": None,
            "last_latency_ms": 12.5,
            "last_audio_bytes": len(self.mp3),
            "last_error": None,
        }


class FakeConverter:
    """生成可控时长的假 PCM（48 kHz / 16bit / 2ch）。"""

    def __init__(self, duration_seconds: float = 0.24, error: Exception | None = None):
        self.duration_seconds = duration_seconds
        self.error = error
        self.chunk_bytes = 11520
        self.decode_calls = 0

    def probe(self, payload: bytes) -> dict:
        return {"format_name": "mp3", "duration_seconds": self.duration_seconds, "codec_name": "mp3"}

    def decode(self, payload: bytes) -> PcmAudio:
        self.decode_calls += 1
        if self.error:
            raise self.error
        frames = int(48000 * self.duration_seconds)
        return PcmAudio(b"\x01\x02\x03\x04" * frames, 48000, 2, 2)


class FakePublisher:
    def __init__(self, chunk_delay: float = 0.0, error: Exception | None = None):
        self.chunk_delay = chunk_delay
        self.error = error
        self.packets = []
        self.started = False
        self.media_pid = 4242
        self.speaker_topic = "/audio/speaker_pcm"

    def start(self) -> bool:
        self.started = True
        return True

    def stop(self) -> None:
        self.started = False

    def status(self) -> dict:
        return {
            "available": True,
            "error": None,
            "node": "test_publisher",
            "topic": "/audio/speaker_pcm",
            "encoding": "pcm_48000_16_2",
            "qos": {"reliability": "reliable", "depth": 10},
            "subscribers": 1,
            "publishers": 0,
            "published_packets": len(self.packets),
            "published_bytes": sum(self.packets),
            "last_publish_at": None,
            "last_error": None,
            "capture": {"topic": "/audio/capture_pcm", "rate_hz": 15.6, "active": True},
            "speaker_status": {"topic": "/audio/speaker_pcm_status", "value": 0, "at": None},
        }

    def media_audio_status(self) -> dict:
        return {"process": "media_audio", "running": True, "pid": self.media_pid, "pids": [self.media_pid]}

    async def publish_pcm(self, pcm, chunk_ms, cancel, on_progress=None, lead_chunks=2):
        if self.error:
            raise self.error
        chunks = pcm.chunks(chunk_ms)
        sent = 0
        for _index, chunk in enumerate(chunks):
            if cancel.is_set():
                break
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            self.packets.append(len(chunk))
            sent += 1
            if on_progress:
                on_progress(
                    {
                        "sent_packets": sent,
                        "sent_bytes": len(chunk) * sent,
                        "remaining_seconds": max(0.0, pcm.duration_seconds - sent * chunk_ms / 1000.0),
                    }
                )
        return {
            "packets": sent,
            "expected_packets": len(chunks),
            "dropped_packets": len(chunks) - sent,
            "chunk_bytes": len(chunks[0]) if chunks else 0,
            "bytes": sent * (len(chunks[0]) if chunks else 0),
            "duration_seconds": sent * chunk_ms / 1000.0,
            "rate_hz": 16.67,
            "measured_rate_hz": 16.67,
            "lag_events": 0,
            "completed": not cancel.is_set(),
            "target": "/audio/speaker_pcm",
            "encoding": "pcm_48000_16_2",
            "media_audio_pid_before": self.media_pid,
            "media_audio_pid_after": self.media_pid,
            "media_audio_pid_unchanged": True,
        }

    def publish_chunk(self, chunk: bytes) -> dict:
        if self.error:
            raise self.error
        if not self.started:
            raise ServiceError("ros_unavailable", "ROS 不可用", 503)
        self.packets.append(len(chunk))
        return {"bytes": len(chunk), "topic": self.speaker_topic, "encoding": "pcm_48000_16_2"}


def make_service(config=None, provider=None, converter=None, publisher=None) -> TtsService:
    return TtsService(
        config or base_config(),
        provider=provider if provider is not None else FakeProvider(),
        converter=converter if converter is not None else FakeConverter(),
        publisher=publisher if publisher is not None else FakePublisher(),
    )


async def wait_terminal(service: TtsService, task_id: str, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        snapshot = service.get_task(task_id)
        if snapshot["phase"] in ("succeeded", "failed", "cancelled"):
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} 未在 {timeout}s 内结束：{service.get_task(task_id)}")


@pytest.fixture
def anyio_backend():
    return "asyncio"
