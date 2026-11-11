"""Vendor preset-audio event routing and fail-safe behaviour."""
from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.vendor_event_routes import create_vendor_event_router
from vendor_event_bridge import VendorEventBridge


class FakeMessage:
    def __init__(self):
        self.data = None


class FakePublisher:
    def __init__(self, topic):
        self.topic = topic
        self.messages = []

    def publish(self, message):
        self.messages.append(message.data)


class FakeSubscription:
    def __init__(self, topic, callback):
        self.topic = topic
        self.callback = callback


class FakeNode:
    def __init__(self):
        self.destroyed = []

    def destroy_subscription(self, handle):
        self.destroyed.append(handle)

    def destroy_publisher(self, handle):
        self.destroyed.append(handle)


class FakeRuntime:
    def __init__(self):
        self.available = True
        self.error = None
        self.node = FakeNode()
        self.publishers = {}
        self.subscriptions = {}

    def start(self):
        self.available = True
        return True

    def create_publisher(self, _message_type, topic, _qos):
        publisher = FakePublisher(topic)
        self.publishers[topic] = publisher
        return publisher

    def create_subscription(self, _message_type, topic, callback, _qos):
        subscription = FakeSubscription(topic, callback)
        self.subscriptions[topic] = subscription
        return subscription


def install_fake_ros(monkeypatch):
    rclpy = types.ModuleType("rclpy")
    qos = types.ModuleType("rclpy.qos")
    qos.HistoryPolicy = types.SimpleNamespace(KEEP_LAST="keep_last")
    qos.ReliabilityPolicy = types.SimpleNamespace(RELIABLE="reliable")
    qos.QoSProfile = lambda **values: values
    std_msgs = types.ModuleType("std_msgs")
    std_msgs_msg = types.ModuleType("std_msgs.msg")
    std_msgs_msg.String = type("String", (FakeMessage,), {})
    std_msgs_msg.Int32 = type("Int32", (FakeMessage,), {})
    for name, module in {
        "rclpy": rclpy,
        "rclpy.qos": qos,
        "std_msgs": std_msgs,
        "std_msgs.msg": std_msgs_msg,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def config(**overrides):
    values = {
        "enabled": True,
        "input_topic": "/audio/speaker_pcm_interrupt",
        "playback_topic": "/audio/vendor_speaker_pcm_interrupt",
        "speaker_status_topic": "/audio/speaker_pcm_status",
        "decision_timeout_seconds": 0.05,
        "agent_stale_seconds": 5.0,
    }
    values.update(overrides)
    return values


async def test_offline_agent_falls_back_to_vendor_audio(monkeypatch):
    install_fake_ros(monkeypatch)
    runtime = FakeRuntime()
    bridge = VendorEventBridge(config(), runtime)
    assert await bridge.start() is True
    try:
        result = await bridge.inject("0092")
        assert result["action"] == "play_vendor"
        assert runtime.publishers[bridge.playback_topic].messages == ["0092"]
        assert runtime.publishers[bridge.status_topic].messages == []
        assert bridge.status()["stats"]["fallback"] == 1
    finally:
        await bridge.stop()


async def test_same_domain_and_topic_is_rejected(monkeypatch):
    install_fake_ros(monkeypatch)
    runtime = FakeRuntime()
    bridge = VendorEventBridge(config(playback_topic="/audio/speaker_pcm_interrupt"), runtime)
    assert await bridge.start() is False
    assert "different ROS domains or topics" in bridge.status()["error"]
    assert runtime.publishers == {}


async def test_full_agent_queue_falls_back_without_hanging(monkeypatch):
    install_fake_ros(monkeypatch)
    runtime = FakeRuntime()
    bridge = VendorEventBridge(config(), runtime)
    await bridge.start()
    try:
        agent = bridge.attach_agent("busy-agent")
        while not agent.queue.full():
            agent.queue.put_nowait({"type": "queued"})
        result = await asyncio.wait_for(bridge.inject("0092"), timeout=0.5)
        assert result["reason"] == "agent_queue_full"
        assert runtime.publishers[bridge.playback_topic].messages == ["0092"]
        assert bridge.status()["pending"] == 0
    finally:
        await bridge.stop()


async def test_agent_decision_suppresses_vendor_audio_and_acks_tree(monkeypatch):
    install_fake_ros(monkeypatch)
    runtime = FakeRuntime()
    bridge = VendorEventBridge(config(), runtime)
    await bridge.start()
    try:
        agent = bridge.attach_agent("m1-primary")
        task = asyncio.create_task(bridge.inject("0051"))
        event = await asyncio.wait_for(agent.queue.get(), timeout=1)
        assert event["event"] == "self_fall_recovery"
        assert event["suggested_delivery"] == "interrupt"
        assert bridge.decide(agent.token, event["event_id"], "agent", "gateway accepted") is True
        result = await task
        assert result["action"] == "agent"
        assert runtime.publishers[bridge.playback_topic].messages == []
        assert runtime.publishers[bridge.status_topic].messages == [1]
        assert bridge.status()["stats"]["agent_handled"] == 1
    finally:
        await bridge.stop()


async def test_missing_decision_times_out_to_vendor_audio(monkeypatch):
    install_fake_ros(monkeypatch)
    runtime = FakeRuntime()
    bridge = VendorEventBridge(config(decision_timeout_seconds=0.01), runtime)
    await bridge.start()
    try:
        bridge.attach_agent("m1-primary")
        result = await bridge.inject("0093")
        assert result["action"] == "play_vendor"
        assert result["reason"] == "decision_timeout"
        assert runtime.publishers[bridge.playback_topic].messages == ["0093"]
        assert bridge.status()["stats"]["timeouts"] == 1
    finally:
        await bridge.stop()


def test_websocket_routes_event_and_accepts_ignore_decision(monkeypatch):
    install_fake_ros(monkeypatch)
    runtime = FakeRuntime()
    bridge = VendorEventBridge(config(decision_timeout_seconds=1.0), runtime)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await bridge.start()
        try:
            yield
        finally:
            await bridge.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_vendor_event_router(bridge))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/vendor/events") as ws:
            ws.send_json({"type": "agent_hello", "instance_id": "m1-primary", "ready": True})
            assert ws.receive_json()["type"] == "vendor_event_ready"
            message = type("String", (), {"data": "0014"})()
            runtime.subscriptions[bridge.input_topic].callback(message)
            event = ws.receive_json()
            assert event["event"] == "vendor_wake"
            ws.send_json({
                "type": "vendor_event_decision",
                "event_id": event["event_id"],
                "action": "ignore",
                "reason": "agent handles microphone input directly",
            })
            decision_ack = ws.receive_json()
            assert decision_ack["accepted"] is True
            deadline = time.monotonic() + 1
            while bridge.status()["stats"]["ignored"] != 1 and time.monotonic() < deadline:
                time.sleep(0.01)

    assert runtime.publishers[bridge.playback_topic].messages == []
    assert runtime.publishers[bridge.status_topic].messages == [1]
