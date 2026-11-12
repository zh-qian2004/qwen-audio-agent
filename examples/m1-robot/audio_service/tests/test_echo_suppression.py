from __future__ import annotations

import audioop
import math
import struct

import pytest

from echo_suppression import (
    FRAME_BYTES,
    FRAME_MS,
    HEADER,
    PACKET_END,
    PACKET_PCM,
    EchoReferenceSender,
    EchoSuppressor,
)


def tone(rate: int, duration_ms: int, frequency: float, amplitude: int) -> bytes:
    samples = rate * duration_ms // 1000
    values = [
        int(amplitude * math.sin(2.0 * math.pi * frequency * index / rate))
        for index in range(samples)
    ]
    return struct.pack(f"<{samples}h", *values)


def stereo(mono: bytes) -> bytes:
    return audioop.tostereo(mono, 2, 1.0, 1.0)


def config(**overrides):
    values = {
        "enabled": True,
        "reference_delay_ms": 250,
        "reference_search_ms": 40,
        "reference_window_ms": 1000,
        "correlation_threshold": 0.65,
        "echo_attenuation_db": 15,
        "near_end_margin_db": 6,
        "near_end_frames": 3,
        "near_end_hold_ms": 200,
        "near_end_min_rms": 400,
        "tail_ms": 250,
        "initial_echo_gain": 0.12,
    }
    values.update(overrides)
    return values


def test_idle_microphone_is_bit_exact_and_unbuffered():
    suppressor = EchoSuppressor(config())
    frame = tone(16000, 20, 440, 4000)
    assert len(frame) == FRAME_BYTES
    assert suppressor.process(frame, 1_000_000_000) == frame
    status = suppressor.status()
    assert status["last"]["state"] == "idle"
    assert status["metrics"]["suppressed_frames"] == 0


def test_correlated_playback_echo_is_attenuated():
    suppressor = EchoSuppressor(config())
    reference = tone(16000, 20, 440, 12000)
    microphone_echo = audioop.mul(reference, 2, 0.12)
    reference_at = 2_000_000_000
    suppressor.ingest_reference(reference, reference_at, 1)

    result = suppressor.process(microphone_echo, reference_at + 250_000_000)

    assert audioop.rms(result, 2) < audioop.rms(microphone_echo, 2) * 0.25
    status = suppressor.status()
    assert status["last"]["state"] == "suppressed"
    assert status["last"]["correlation"] > 0.99
    assert status["metrics"]["suppressed_frames"] == 1


def test_near_end_speech_passes_after_confirmation_frames():
    suppressor = EchoSuppressor(config())
    reference = tone(16000, 20, 440, 12000)
    echo = audioop.mul(reference, 2, 0.12)
    user = tone(16000, 20, 997, 9000)
    mixed = audioop.add(echo, user, 2)
    start = 3_000_000_000

    results = []
    for index in range(3):
        reference_at = start + index * FRAME_MS * 1_000_000
        suppressor.ingest_reference(reference, reference_at, index + 1)
        results.append(suppressor.process(mixed, reference_at + 250_000_000))

    assert audioop.rms(results[0], 2) < audioop.rms(mixed, 2) * 0.25
    assert audioop.rms(results[1], 2) < audioop.rms(mixed, 2) * 0.25
    assert results[2] == mixed
    status = suppressor.status()
    assert status["last"]["state"] == "near_end"
    assert status["metrics"]["near_end_frames"] == 1


def test_sender_converts_rendered_pcm_to_reference_datagrams():
    class FakeSocket:
        def __init__(self):
            self.packets = []

        def sendto(self, packet, address):
            self.packets.append((packet, address))
            return len(packet)

        def close(self):
            pass

    fake = FakeSocket()
    sender = EchoReferenceSender(config(reference_port=19994), socket_impl=fake)
    sender.begin("stream-1")
    rendered = stereo(tone(48000, 120, 330, 8000))
    sender.publish_48k_stereo(rendered, 4_000_000_000)
    sender.end("stream-1", 4_120_000_000)

    pcm_packets = [packet for packet, _ in fake.packets if HEADER.unpack_from(packet)[1] == PACKET_PCM]
    end_packets = [packet for packet, _ in fake.packets if HEADER.unpack_from(packet)[1] == PACKET_END]
    assert len(pcm_packets) >= 5
    assert len(end_packets) == 1
    assert all(len(packet) == HEADER.size + FRAME_BYTES for packet in pcm_packets)
    timestamps = [HEADER.unpack_from(packet)[2] for packet in pcm_packets]
    assert timestamps == sorted(timestamps)
    assert all(right - left == FRAME_MS * 1_000_000 for left, right in zip(timestamps, timestamps[1:]))
    assert sender.status()["errors"] == 0


def test_invalid_reference_packet_is_rejected():
    suppressor = EchoSuppressor(config())
    assert suppressor.ingest_packet(b"broken") is False
    assert suppressor.status()["metrics"]["invalid_reference_packets"] == 1


def test_disabled_suppressor_is_passthrough():
    suppressor = EchoSuppressor(config(enabled=False))
    frame = tone(16000, 20, 440, 4000)
    assert suppressor.process(frame, 1) == frame
    assert suppressor.start() is False
