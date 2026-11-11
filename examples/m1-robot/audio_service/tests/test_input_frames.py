"""输入侧：帧模型、ROS 消息校验、20 ms 重分帧、样本守恒、环形缓冲。"""
from __future__ import annotations

import struct
import time

import pytest

from input.audio_frame import AudioFormat, AudioFrame, make_frame, parse_encoding
from input.rechunker import Rechunker
from input.ring_buffer import RingBuffer
from input.ros_capture import RosCaptureSubscriber


def tone_pcm(samples: int, amplitude: int = 1000) -> bytes:
    values = [amplitude if index % 2 == 0 else -amplitude for index in range(samples)]
    return struct.pack(f"<{samples}h", *values)


class FakeStamp:
    def __init__(self, sec: int, nanosec: int = 0):
        self.sec = sec
        self.nanosec = nanosec


class FakeHeader:
    def __init__(self, sec: int = 1700000000):
        self.stamp = FakeStamp(sec)


class FakeImage:
    def __init__(
        self,
        data: bytes,
        encoding: str = "pcm_16000_16_1",
        height: int = 1,
        step: int | None = None,
        width: int | None = None,
        bigendian: bool = False,
        sec: int = 1700000000,
    ):
        self.data = data
        self.encoding = encoding
        self.height = height
        self.step = len(data) if step is None else step
        self.width = len(data) if width is None else width
        self.is_bigendian = bigendian
        self.header = FakeHeader(sec)


# ---- 1. sensor_msgs/Image 格式验证 ---------------------------------
def make_capture():
    frames = []
    capture = RosCaptureSubscriber(
        {"topic": "/audio/capture_pcm", "encoding": "pcm_16000_16_1", "ingress_capacity": 8},
        on_frame=lambda frame: (frames.append(frame), capture.ingress.push(frame))[0],
        runtime=None,
    )
    capture._started = True
    return capture, frames


def test_capture_accepts_valid_image():
    capture, frames = make_capture()
    capture._on_message(FakeImage(tone_pcm(1024)))
    assert len(frames) == 1
    assert capture.received_packets == 1
    assert capture.invalid_packets == 0
    frame = frames[0]
    assert frame.sample_rate == 16000 and frame.channels == 1 and frame.sample_width == 2
    assert frame.byte_length == 2048
    assert frame.source_timestamp_ns == 1700000000 * 1_000_000_000


@pytest.mark.parametrize(
    "message,reason",
    [
        (FakeImage(tone_pcm(1024), encoding="pcm_48000_16_1"), "sample_rate_48000"),
        (FakeImage(tone_pcm(1024), encoding="pcm_16000_16_2"), "channels_2"),
        (FakeImage(tone_pcm(1024), encoding="pcm_16000_8_1"), "sample_width_8bit"),
        (FakeImage(tone_pcm(1024), encoding="pcm_16000_24_1"), "sample_width_24bit"),
        (FakeImage(tone_pcm(1024), encoding="float32"), "encoding_unparsable"),
        (FakeImage(tone_pcm(1024), bigendian=True), "big_endian_unsupported"),
        (FakeImage(tone_pcm(1024), step=1024), "shape_mismatch"),
        (FakeImage(tone_pcm(1024)[:-1]), "invalid_payload"),
    ],
)
def test_capture_rejects_invalid_images(message, reason):
    capture, frames = make_capture()
    capture._on_message(message)
    assert frames == []
    assert capture.invalid_packets == 1
    assert capture.last_reason and capture.last_reason.startswith(reason.split(":")[0])
    assert capture.received_packets == 0


def test_capture_ingress_drops_oldest_when_full():
    capture, frames = make_capture()
    for index in range(12):
        capture._on_message(FakeImage(tone_pcm(1024), sec=1700000000 + index))
    assert len(frames) == 12  # 回调本身不做丢弃
    assert capture.ingress.depth() == 8
    assert capture.ingress.dropped == 4
    assert [frame.input_sequence for frame in capture.ingress.drain()][-1] == 12


# ---- 2. S16_LE 单声道解析 -----------------------------------------
def test_s16le_mono_parse_and_frame_model():
    assert parse_encoding("pcm_16000_16_1") == AudioFormat(16000, 1, 2)
    assert parse_encoding("PCM_48000_16_2") == AudioFormat(48000, 2, 2)
    assert parse_encoding("pcm_16000") is None

    pcm = struct.pack("<4h", 1, -2, 3, -4)
    fmt = AudioFormat(16000, 1, 2)
    frame = make_frame(sequence=7, pcm=pcm, fmt=fmt, captured_at_ns=99, input_sequence=3)
    assert frame.sample_count == 4
    assert frame.duration_ms == pytest.approx(0.25)
    assert frame.encoding == "pcm_16000_16_1"
    assert frame.continuity() == {"sequence": 7, "input_sequence": 3, "discontinuity": False}
    assert isinstance(frame, AudioFrame)


def test_invalid_payload_validation():
    fmt = AudioFormat(16000, 1, 2)
    with pytest.raises(ValueError):
        fmt.validate_payload(b"\x00")
    with pytest.raises(ValueError):
        fmt.validate_payload(b"")


# ---- 3. 64 ms → 20 ms 跨包重分帧 ----------------------------------
def test_rechunker_splits_64ms_packets_into_20ms():
    rechunker = Rechunker(16000, 1, 2, frame_ms=20)
    assert rechunker.frame_bytes == 640
    outputs = []
    for _ in range(5):
        outputs.extend(rechunker.feed(tone_pcm(1024)))
    assert len(outputs) == 16
    assert {len(item) for item in outputs} == {640}
    assert rechunker.buffered_bytes == 0
    assert rechunker.output_frames == 16


def test_rechunker_keeps_remainder_between_packets():
    rechunker = Rechunker(16000, 1, 2, frame_ms=20)
    first = rechunker.feed(tone_pcm(1024))
    assert len(first) == 3
    assert rechunker.buffered_bytes == 128  # 64 个样本余数
    second = rechunker.feed(tone_pcm(1024))
    assert len(second) == 3
    assert rechunker.buffered_bytes == 256


def test_rechunker_flush_reports_remaining_bytes():
    rechunker = Rechunker(16000, 1, 2, frame_ms=20)
    rechunker.feed(tone_pcm(1024))
    assert rechunker.flush() == 128
    assert rechunker.buffered_bytes == 0


# ---- 4. 样本数完全守恒 --------------------------------------------
def test_rechunker_sample_conservation_and_order():
    rechunker = Rechunker(16000, 1, 2, frame_ms=20)
    packets = [bytes([(index + offset) % 251 for offset in range(2048)]) for index in range(5)]
    outputs = []
    for packet in packets:
        outputs.extend(rechunker.feed(packet))
    combined = b"".join(outputs)
    expected = b"".join(packets)
    assert combined == expected
    total_samples = sum(len(item) for item in outputs) // 2
    assert total_samples == 5 * 1024
    # 跨包边界不重复、不缺失：相邻输出帧首尾样本连续递增
    assert combined[0:2] == expected[0:2]
    assert combined[-2:] == expected[-2:]


def test_rechunker_survives_unaligned_tail():
    rechunker = Rechunker(16000, 1, 2, frame_ms=20)
    assert rechunker.feed(b"\x00") == []
    assert rechunker.last_error
    assert rechunker.feed(tone_pcm(320)) != []


# ---- 5. 环形缓冲容量与 pre-roll -----------------------------------
def build_frame(sequence: int, samples: int = 320) -> AudioFrame:
    return make_frame(
        sequence=sequence,
        pcm=tone_pcm(samples),
        fmt=AudioFormat(16000, 1, 2),
        captured_at_ns=time.monotonic_ns(),
    )


def test_ring_buffer_capacity_and_preroll():
    ring = RingBuffer(frame_ms=20, capacity_ms=5000)
    assert ring.capacity_frames == 250
    for sequence in range(1, 301):
        ring.append(build_frame(sequence))
    assert ring.count == 250
    assert ring.available_ms == 5000
    assert ring.last_frame().sequence == 300

    recent = ring.recent(1000)
    assert len(recent) == 50
    assert recent[0].sequence == 251
    assert recent[-1].sequence == 300

    assert [frame.sequence for frame in ring.latest(2)] == [299, 300]
    assert ring.recent(0) == []
    status = ring.status()
    assert status["capacity_ms"] == 5000 and status["available_frames"] == 250


def test_ring_buffer_empty_and_clear():
    ring = RingBuffer(frame_ms=20, capacity_ms=100)
    assert ring.latest(3) == []
    assert ring.last_frame() is None
    ring.append(build_frame(1))
    ring.clear()
    assert ring.available_ms == 0


def test_rechunker_invalid_frame_ms():
    with pytest.raises(ValueError):
        Rechunker(16000, 1, 2, frame_ms=0)
    with pytest.raises(ValueError):
        Rechunker(44100, 1, 2, frame_ms=7)
