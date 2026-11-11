"""PCM 格式校验、60 ms 分包、ROS 发布节奏与真实 ffmpeg 解码。"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from conftest import base_config
from audio_converter import AudioConverter, PcmAudio
from ros_pcm_publisher import RosPcmPublisher
from state import ServiceError


class FakeImage:
    def __init__(self):
        self.header = SimpleNamespace(stamp=None, frame_id="")
        self.height = 0
        self.width = 0
        self.encoding = ""
        self.is_bigendian = True
        self.step = 0
        self.data = b""


class FakeRawPublisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message)


class FakeNode:
    def get_clock(self):
        return SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: "stamp"))


def make_publisher() -> RosPcmPublisher:
    publisher = RosPcmPublisher(base_config()["ros"])
    publisher._started = True
    publisher._publisher = FakeRawPublisher()
    publisher._image_cls = FakeImage
    publisher._node = FakeNode()
    return publisher


def test_pcm_rejects_wrong_sample_rate():
    pcm = PcmAudio(b"\x00" * 4 * 100, 44100, 2, 2)
    with pytest.raises(ServiceError) as info:
        pcm.validate()
    assert info.value.code == "pcm_format_invalid"


def test_pcm_rejects_wrong_channels():
    pcm = PcmAudio(b"\x00" * 2 * 100, 48000, 1, 2)
    with pytest.raises(ServiceError) as info:
        pcm.validate()
    assert info.value.code == "pcm_format_invalid"


def test_pcm_rejects_misaligned_payload():
    pcm = PcmAudio(b"\x00" * 7, 48000, 2, 2)
    with pytest.raises(ServiceError) as info:
        pcm.validate()
    assert info.value.code == "pcm_format_invalid"


def test_chunk_size_is_11520_bytes():
    converter = AudioConverter(base_config()["audio"])
    assert converter.chunk_bytes == 11520

    pcm = PcmAudio(b"\x00" * 4 * 48000, 48000, 2, 2)  # 1 秒
    chunks = pcm.chunks(60)
    assert len(chunks) == 17  # ceil(48000 frames / 2880 frames)
    assert all(len(chunk) == 11520 for chunk in chunks[:-1])
    assert len(chunks[-1]) == 4 * (48000 - 16 * 2880)


def test_last_chunk_stays_frame_aligned():
    pcm = PcmAudio(b"\x00" * (11520 * 2 + 3), 48000, 2, 2)
    chunks = pcm.chunks(60)
    assert len(chunks) == 3
    assert all(len(chunk) % pcm.frame_bytes == 0 for chunk in chunks)
    assert len(chunks[-1]) == 4


def test_pcm_format_dict():
    pcm = PcmAudio(b"\x00" * 4 * 48000, 48000, 2, 2)
    info = pcm.format_dict()
    assert info == {
        "sample_rate": 48000,
        "sample_format": "s16le",
        "channels": 2,
        "bytes": 192000,
        "frames": 48000,
        "frame_bytes": 4,
        "duration_seconds": 1.0,
        "encoding": "pcm_48000_16_2",
    }


async def test_publish_pacing_is_realtime():
    publisher = make_publisher()
    pcm = PcmAudio(b"\x00" * 4 * 48000, 48000, 2, 2)  # 1 秒 -> 17 包
    cancel = asyncio.Event()
    stats = await publisher.publish_pcm(pcm, 60, cancel, lead_chunks=2)

    assert stats["packets"] == 17
    assert stats["expected_packets"] == 17
    assert stats["dropped_packets"] == 0
    assert stats["chunk_bytes"] == 11520
    assert stats["rate_hz"] == 16.67
    assert stats["measured_rate_hz"] == pytest.approx(20.2, abs=2.0)
    # 前 2 包立即发出，最后一包大约在 (17-1-2)*60ms = 0.84s
    assert 0.7 <= stats["duration_seconds"] <= 1.15
    assert stats["completed"] is True
    assert stats["media_audio_pid_before"] == stats["media_audio_pid_after"]

    messages = publisher._publisher.messages
    assert len(messages) == 17
    assert all(m.height == 1 for m in messages)
    assert all(m.width == len(m.data) for m in messages)
    assert all(m.step == len(m.data) for m in messages)
    assert all(m.encoding == "pcm_48000_16_2" for m in messages)
    assert all(m.is_bigendian is False for m in messages)
    assert sum(len(m.data) for m in messages) == 192000


async def test_publish_respects_cancel():
    publisher = make_publisher()
    pcm = PcmAudio(b"\x00" * 4 * 48000, 48000, 2, 2)
    cancel = asyncio.Event()
    cancel.set()
    stats = await publisher.publish_pcm(pcm, 60, cancel)
    assert stats["packets"] == 0
    assert stats["dropped_packets"] == 17
    assert stats["completed"] is False
    assert publisher._publisher.messages == []


async def test_publish_without_ros_raises():
    publisher = RosPcmPublisher(base_config()["ros"])
    pcm = PcmAudio(b"\x00" * 4, 48000, 2, 2)
    with pytest.raises(ServiceError) as info:
        await publisher.publish_pcm(pcm, 60, asyncio.Event())
    assert info.value.code == "ros_unavailable"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg")
def test_real_ffmpeg_decodes_mp3_to_48k_stereo(tmp_path):
    mp3_path = tmp_path / "probe.mp3"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3:sample_rate=24000",
            "-ac", "1", "-b:a", "64k", str(mp3_path),
        ],
        check=True,
    )
    payload = mp3_path.read_bytes()
    converter = AudioConverter(base_config()["audio"])
    pcm = converter.decode(payload)
    assert pcm.sample_rate == 48000
    assert pcm.channels == 2
    assert pcm.sample_width == 2
    assert len(pcm.data) % pcm.frame_bytes == 0
    assert 0.2 <= pcm.duration_seconds <= 0.45
    assert converter.probe(payload)["format_name"] == "mp3"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机没有 ffmpeg")
def test_real_ffmpeg_rejects_invalid_payload():
    converter = AudioConverter(base_config()["audio"])
    with pytest.raises(ServiceError) as info:
        converter.decode(b"this is not an mp3 file")
    assert info.value.code == "decode_failed"
