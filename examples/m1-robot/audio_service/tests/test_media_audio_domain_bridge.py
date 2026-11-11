"""Selective media_audio private-domain topic relay."""
from __future__ import annotations

import sys
import types
import asyncio

import pytest

from media_audio_domain_bridge import MediaAudioDomainBridge


class FakePublisher:
    def __init__(self, topic):
        self.topic = topic
        self.messages = []

    def publish(self, message):
        self.messages.append(message)


class FakeSubscription:
    def __init__(self, topic, callback):
        self.topic = topic
        self.callback = callback


class FakeNode:
    def __init__(self):
        self.clients = {}
        self.services = {}
        self.timers = []
        self.destroyed = []

    def create_client(self, service_type, name, **kwargs):
        client = FakeClient(service_type, name)
        self.clients[name] = client
        return client

    def create_service(self, service_type, name, callback, **kwargs):
        service = types.SimpleNamespace(name=name, service_type=service_type, callback=callback)
        self.services[name] = service
        return service

    def create_timer(self, period, callback, **kwargs):
        timer = FakeTimer(period, callback)
        self.timers.append(timer)
        return timer

    def destroy_timer(self, timer):
        self.destroyed.append(timer)

    def destroy_client(self, client):
        self.destroyed.append(client)

    def destroy_service(self, service):
        self.destroyed.append(service)

    def destroy_publisher(self, _handle):
        pass

    def destroy_subscription(self, _handle):
        pass


class FakeClient:
    def __init__(self, service_type, name):
        self.service_type = service_type
        self.name = name
        self.ready = True
        self.requests = []
        self.futures = []
        self.removed = []
        self.error = None

    def service_is_ready(self):
        return self.ready

    def call_async(self, request):
        if self.error:
            raise self.error
        future = asyncio.get_running_loop().create_future()
        self.requests.append(request)
        self.futures.append(future)
        return future

    def remove_pending_request(self, future):
        self.removed.append(future)


class FakeTimer:
    def __init__(self, period, callback):
        self.period = period
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class Trigger:
    class Request:
        pass

    class Response:
        def __init__(self):
            self.success = False
            self.message = ""


class VolumeControl:
    class Request:
        def __init__(self):
            self.command = ""

    class Response:
        def __init__(self):
            self.success = False
            self.message = ""


class FakeRuntime:
    def __init__(self, name):
        self.node_name = name
        self.node = FakeNode()
        self.available = True
        self.error = None
        self.publishers = {}
        self.subscriptions = {}

    def start(self):
        self.available = True
        return True

    def stop(self):
        self.available = False

    def create_publisher(self, _message_type, topic, _qos):
        publisher = FakePublisher(topic)
        self.publishers[topic] = publisher
        return publisher

    def create_subscription(self, _message_type, topic, callback, _qos):
        subscription = FakeSubscription(topic, callback)
        self.subscriptions[topic] = subscription
        return subscription

    def status(self):
        return {"available": self.available, "node": self.node_name}


def install_messages(monkeypatch):
    qos = types.ModuleType("rclpy.qos")
    qos.HistoryPolicy = types.SimpleNamespace(KEEP_LAST="keep_last")
    qos.ReliabilityPolicy = types.SimpleNamespace(RELIABLE="reliable")
    qos.QoSProfile = lambda **values: values
    sensor_msgs = types.ModuleType("sensor_msgs")
    sensor_msgs_msg = types.ModuleType("sensor_msgs.msg")
    sensor_msgs_msg.Image = type("Image", (), {})
    std_msgs = types.ModuleType("std_msgs")
    std_msgs_msg = types.ModuleType("std_msgs.msg")
    std_msgs_msg.Int32 = type("Int32", (), {})
    std_msgs_msg.Int32MultiArray = type("Int32MultiArray", (), {})
    std_msgs_msg.String = type("String", (), {})
    callback_groups = types.ModuleType("rclpy.callback_groups")
    callback_groups.ReentrantCallbackGroup = type("ReentrantCallbackGroup", (), {})
    clock = types.ModuleType("rclpy.clock")
    clock.ClockType = types.SimpleNamespace(STEADY_TIME="steady")
    clock.Clock = lambda **kwargs: kwargs
    task = types.ModuleType("rclpy.task")
    task.Future = lambda: asyncio.get_running_loop().create_future()
    std_srvs = types.ModuleType("std_srvs.srv")
    std_srvs.Trigger = Trigger
    video_ros_msgs = types.ModuleType("video_ros_msgs.srv")
    video_ros_msgs.VolumeControl = VolumeControl
    for name, module in {
        "rclpy.qos": qos,
        "sensor_msgs": sensor_msgs,
        "sensor_msgs.msg": sensor_msgs_msg,
        "std_msgs": std_msgs,
        "std_msgs.msg": std_msgs_msg,
        "rclpy.callback_groups": callback_groups,
        "rclpy.clock": clock,
        "rclpy.task": task,
        "std_srvs.srv": std_srvs,
        "video_ros_msgs.srv": video_ros_msgs,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
async def running_bridge(monkeypatch):
    install_messages(monkeypatch)
    host = FakeRuntime("host")
    media = FakeRuntime("media")
    bridge = MediaAudioDomainBridge(
        {"enabled": True, "domain_id": 43, "service_timeout_seconds": 0.25},
        host, media_runtime=media,
    )
    assert await bridge.start()
    try:
        yield bridge, host, media
    finally:
        await bridge.stop()


async def test_selective_routes_exclude_vendor_interrupt(monkeypatch):
    install_messages(monkeypatch)
    host = FakeRuntime("host")
    media = FakeRuntime("media")
    bridge = MediaAudioDomainBridge(
        {"enabled": True, "domain_id": 43}, host, media_runtime=media
    )
    assert await bridge.start() is True
    try:
        assert "/audio/capture_pcm" in media.subscriptions
        assert "/audio/capture_pcm" in host.publishers
        assert "/audio/speaker_pcm" in host.subscriptions
        assert "/audio/speaker_pcm" in media.publishers
        assert "/audio/speaker_pcm_interrupt" not in host.subscriptions
        assert "/audio/speaker_pcm_interrupt" not in media.publishers
        assert "/audio/speaker_pcm_interrupt" not in media.subscriptions
        assert "/all_enable" not in host.node.services
        assert "/all_disable" not in host.node.services
        assert len(host.node.services) == 8

        capture = object()
        media.subscriptions["/audio/capture_pcm"].callback(capture)
        assert host.publishers["/audio/capture_pcm"].messages == [capture]

        speaker = object()
        host.subscriptions["/audio/speaker_pcm"].callback(speaker)
        assert media.publishers["/audio/speaker_pcm"].messages == [speaker]

        for topic in ("/SystemWiFiStatus", "/audio/speaker_control"):
            command = object()
            host.subscriptions[topic].callback(command)
            assert media.publishers[topic].messages == [command]
        original_pcm = object()
        media.subscriptions["/audio/capture_pcm_origin"].callback(original_pcm)
        assert host.publishers["/audio/capture_pcm_origin"].messages == [original_pcm]
    finally:
        await bridge.stop()


async def test_service_preserves_request_response_while_capture_continues(running_bridge):
    bridge, host, media = running_bridge
    name = "/speaker_volume_control"
    request = VolumeControl.Request()
    request.command = "37"
    callback = host.node.services[name].callback
    task = asyncio.create_task(callback(request, VolumeControl.Response()))
    await asyncio.sleep(0)
    client = media.node.clients[name]
    assert client.requests == [request]
    assert not task.done()

    # Waiting on the service must leave the PCM relay usable.
    pcm = object()
    media.subscriptions["/audio/capture_pcm"].callback(pcm)
    assert host.publishers["/audio/capture_pcm"].messages == [pcm]

    response = VolumeControl.Response()
    response.success = True
    response.message = "volume=37"
    client.futures[0].set_result(response)
    assert await task is response
    timer = host.node.timers[0]
    assert timer.cancelled and timer in host.node.destroyed
    # A queued timer callback after a reply cannot replace the result.
    timer.fire()
    assert bridge.status()["services"][name]["completed"] == 1
    assert bridge.status()["services"][name]["timeouts"] == 0
    assert bridge.status()["pending_services"] == 0


async def test_service_timeout_cleans_up_request_and_returns_failure(running_bridge):
    bridge, host, media = running_bridge
    name = "/audio/set_mode_aec3"
    callback = host.node.services[name].callback
    task = asyncio.create_task(callback(Trigger.Request(), Trigger.Response()))
    await asyncio.sleep(0)
    client = media.node.clients[name]
    timer = host.node.timers[0]
    assert timer.period == 0.25
    timer.fire()
    response = await task
    assert not response.success
    assert "timed out" in response.message
    assert client.futures[0].cancelled()
    assert client.removed == client.futures
    assert timer in host.node.destroyed
    await asyncio.sleep(0)  # The cancelled client future's callback is harmless.
    assert bridge.status()["services"][name]["timeouts"] == 1
    assert bridge.status()["services"][name]["errors"] == 0
    assert bridge.status()["pending_services"] == 0


async def test_unavailable_service_returns_without_creating_timer(running_bridge):
    bridge, host, media = running_bridge
    name = "/speaker_enable"
    media.node.clients[name].ready = False
    response = await host.node.services[name].callback(Trigger.Request(), Trigger.Response())
    assert not response.success
    assert "unavailable" in response.message
    assert not host.node.timers
    assert not media.node.clients[name].requests
    assert bridge.status()["services"][name]["unavailable"] == 1


async def test_client_error_returns_failure_and_releases_timer(running_bridge):
    bridge, host, media = running_bridge
    name = "/audio_disable"
    media.node.clients[name].error = RuntimeError("DDS unavailable")
    response = await host.node.services[name].callback(Trigger.Request(), Trigger.Response())
    assert not response.success
    assert "DDS unavailable" in response.message
    assert host.node.timers[0] in host.node.destroyed
    assert bridge.status()["services"][name]["errors"] == 1
    assert bridge.status()["pending_services"] == 0


async def test_stop_resolves_inflight_service_and_destroys_handles(running_bridge):
    bridge, host, media = running_bridge
    name = "/audio_enable"
    task = asyncio.create_task(host.node.services[name].callback(Trigger.Request(), Trigger.Response()))
    await asyncio.sleep(0)
    await bridge.stop()
    response = await task
    assert not response.success
    assert "stopping" in response.message
    assert bridge.status()["pending_services"] == 0
    assert bridge.status()["services"][name]["stopped"] == 1
    assert media.node.clients[name] in media.node.destroyed
    assert host.node.services[name] in host.node.destroyed
    assert not media.available
