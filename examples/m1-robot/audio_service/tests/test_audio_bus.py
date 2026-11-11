"""Audio Bus：latest / drop_oldest / fail_on_gap、消费者隔离与资源释放。"""
from __future__ import annotations

import asyncio
import time

import pytest

from input.audio_bus import AudioBus, AudioGapError
from input.audio_frame import AudioFormat, AudioFrame, make_frame

FMT = AudioFormat(16000, 1, 2)
SILENCE = b"\x00\x00" * 320


def frame(sequence: int) -> AudioFrame:
    return make_frame(
        sequence=sequence,
        pcm=SILENCE,
        fmt=FMT,
        captured_at_ns=time.monotonic_ns(),
    )


def test_latest_consumer_only_keeps_newest():
    bus = AudioBus()
    subscription = bus.subscribe("level", policy="latest", capacity=1)
    for sequence in range(1, 6):
        bus.publish(frame(sequence))
    assert subscription.stats()["queue_depth"] == 1
    assert subscription.stats()["dropped_frames"] == 4
    received = asyncio.run(subscription.wait_for(1, timeout=0.2))
    assert [item.sequence for item in received] == [5]
    subscription.close()


def test_drop_oldest_isolated_from_other_consumers():
    bus = AudioBus()
    slow = bus.subscribe("slow", policy="drop_oldest", capacity=3)
    fast = bus.subscribe("fast", policy="latest", capacity=1)
    for sequence in range(1, 11):
        bus.publish(frame(sequence))

    slow_stats = slow.stats()
    assert slow_stats["queue_depth"] == 3
    assert slow_stats["dropped_frames"] == 7
    assert slow_stats["received_frames"] == 10
    # 三个消费者互不影响
    assert fast.stats()["received_frames"] == 10
    slow.close()
    fast.close()


def test_fail_on_gap_reports_structured_event():
    bus = AudioBus()
    subscription = bus.subscribe("asr", policy="fail_on_gap", capacity=10)
    bus.publish(frame(1))
    bus.publish(frame(2))
    bus.publish(frame(5))  # 3、4 缺失

    with pytest.raises(AudioGapError) as excinfo:
        asyncio.run(subscription.__anext__())
    event = excinfo.value.event
    assert event["type"] == "audio_gap"
    assert event["reason"] == "sequence_gap"
    assert event["expected_sequence"] == 3
    assert event["received_sequence"] == 5
    assert subscription.stats()["gaps"] == 1
    subscription.close()


def test_fail_on_gap_detects_queue_overflow():
    bus = AudioBus()
    subscription = bus.subscribe("asr", policy="fail_on_gap", capacity=2)
    for sequence in range(1, 6):
        bus.publish(frame(sequence))
    stats = subscription.stats()
    assert stats["gaps"] == 1
    assert stats["last_gap"]["reason"] == "queue_overflow"
    subscription.close()


def test_fail_on_gap_survives_preroll_seed():
    bus = AudioBus()
    subscription = bus.subscribe("asr", policy="fail_on_gap", capacity=10)
    subscription.seed([frame(1), frame(2)])
    bus.publish(frame(3))
    frames = asyncio.run(subscription.wait_for(3, timeout=0.2))
    assert [item.sequence for item in frames] == [1, 2, 3]
    assert subscription.stats()["gaps"] == 0
    # 重复投递 pre-roll 覆盖的帧会被忽略
    bus.publish(frame(3))
    assert subscription.stats()["duplicate_frames"] == 1
    subscription.close()


async def test_slow_consumer_does_not_block_fast_consumer():
    bus = AudioBus()
    bus.bind_loop(asyncio.get_running_loop())
    slow = bus.subscribe("slow-ws", policy="drop_oldest", capacity=2)
    fast = bus.subscribe("fast-ws", policy="drop_oldest", capacity=100)

    started = time.monotonic()
    for sequence in range(1, 201):
        bus.publish(frame(sequence))
    elapsed = time.monotonic() - started
    assert elapsed < 1.0  # 发布完全是非阻塞的

    fast_frames = await fast.wait_for(100, timeout=0.5)
    assert len(fast_frames) == 100
    assert fast_frames[-1].sequence == 200
    assert slow.stats()["dropped_frames"] == 198
    slow.close()
    fast.close()


async def test_multiple_consumers_receive_same_stream():
    bus = AudioBus()
    bus.bind_loop(asyncio.get_running_loop())
    consumers = [
        bus.subscribe(f"consumer-{index}", policy="drop_oldest", capacity=20)
        for index in range(3)
    ]
    for sequence in range(1, 11):
        bus.publish(frame(sequence))
    for consumer in consumers:
        received = await consumer.wait_for(10, timeout=0.3)
        assert [item.sequence for item in received] == list(range(1, 11))
        consumer.close()


async def test_consumer_close_releases_queue_and_metrics():
    bus = AudioBus()
    bus.bind_loop(asyncio.get_running_loop())
    subscription = bus.subscribe("ws", policy="drop_oldest", capacity=5)
    bus.publish(frame(1))
    assert bus.consumer_count == 1
    subscription.close()
    assert bus.consumer_count == 0
    assert subscription.stats()["queue_depth"] == 0
    with pytest.raises(StopAsyncIteration):
        await subscription.__anext__()


async def test_bus_publish_continues_after_consumer_closed():
    bus = AudioBus()
    bus.bind_loop(asyncio.get_running_loop())
    first = bus.subscribe("first", policy="latest", capacity=1)
    second = bus.subscribe("second", policy="latest", capacity=1)
    first.close()
    bus.publish(frame(1))
    assert second.stats()["queue_depth"] == 1
    assert bus.consumer_count == 1
    second.close()
