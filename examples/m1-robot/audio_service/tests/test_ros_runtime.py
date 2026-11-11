"""ROS 生命周期：单次 rclpy 初始化、输入订阅与 TTS 发布器共享 Node/Executor。

用假的 rclpy / sensor_msgs 模块替代真实 ROS，单元测试不启动 DDS。
"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from input.ros_capture import RosCaptureSubscriber
from ros_pcm_publisher import RosPcmPublisher
from ros_runtime import RosRuntime


class FakeQoSProfile:
    def __init__(self, depth=10, history=None, reliability=None):
        self.depth = depth
        self.history = history
        self.reliability = reliability


class FakeClock:
    def now(self):
        return types.SimpleNamespace(to_msg=lambda: types.SimpleNamespace(sec=1, nanosec=2))


class FakeSubscription:
    def __init__(self, topic, msg_type, callback):
        self.topic = topic
        self.msg_type = msg_type
        self.callback = callback


class FakePublisherHandle:
    def __init__(self, topic):
        self.topic = topic
        self.messages = []

    def publish(self, message):
        self.messages.append(message)


class FakeNode:
    instances: list = []

    def __init__(self, name, start_parameter_services=True, context=None):
        self.name = name
        self.context = context
        self.publishers = []
        self.subscriptions = []
        self.destroyed = False
        FakeNode.instances.append(self)

    def create_publisher(self, msg_type, topic, qos):
        handle = FakePublisherHandle(topic)
        self.publishers.append((topic, msg_type, qos, handle))
        return handle

    def create_subscription(self, msg_type, topic, callback, qos):
        subscription = FakeSubscription(topic, msg_type, callback)
        self.subscriptions.append(subscription)
        return subscription

    def destroy_subscription(self, subscription):
        if subscription in self.subscriptions:
            self.subscriptions.remove(subscription)

    def count_publishers(self, topic):
        return 1

    def count_subscribers(self, topic):
        return 3

    def get_clock(self):
        return FakeClock()

    def destroy_node(self):
        self.destroyed = True


class FakeExecutor:
    def __init__(self, num_threads=1, context=None):
        self.num_threads = num_threads
        self.context = context
        self.nodes = []
        self._stop = threading.Event()

    def add_node(self, node):
        self.nodes.append(node)

    def spin(self):
        self._stop.wait()

    def shutdown(self, timeout_sec=None):
        self._stop.set()


class FakeRclpy(types.ModuleType):
    def __init__(self):
        super().__init__("rclpy")
        self.initialized = False
        self.init_calls = 0
        self.shutdown_calls = 0
        self.spun_nodes = []

    def init(self, args=None, context=None, domain_id=None):
        self.init_calls += 1
        if context is not None:
            context.initialized = True
            context.domain_id = domain_id
        else:
            self.initialized = True

    def ok(self):
        return self.initialized

    def try_shutdown(self):
        self.shutdown_calls += 1
        self.initialized = False

    def spin(self, node):
        self.spun_nodes.append(node)
        while self.initialized:
            time.sleep(0.005)


def install_fake_ros(monkeypatch) -> FakeRclpy:
    fake = FakeRclpy()
    node_module = types.ModuleType("rclpy.node")
    node_module.Node = FakeNode
    executors = types.ModuleType("rclpy.executors")
    executors.MultiThreadedExecutor = FakeExecutor
    executors.SingleThreadedExecutor = FakeExecutor
    context_module = types.ModuleType("rclpy.context")

    class FakeContext:
        def __init__(self):
            self.initialized = False
            self.domain_id = None
            self.shutdown_calls = 0

        def try_shutdown(self):
            self.shutdown_calls += 1
            self.initialized = False

    context_module.Context = FakeContext
    qos = types.ModuleType("rclpy.qos")
    qos.QoSProfile = FakeQoSProfile
    qos.HistoryPolicy = types.SimpleNamespace(KEEP_LAST="keep_last")
    qos.ReliabilityPolicy = types.SimpleNamespace(RELIABLE="reliable", BEST_EFFORT="best_effort")

    sensor_msgs = types.ModuleType("sensor_msgs")
    sensor_msgs_msg = types.ModuleType("sensor_msgs.msg")
    sensor_msgs_msg.Image = type("Image", (), {})
    std_msgs = types.ModuleType("std_msgs")
    std_msgs_msg = types.ModuleType("std_msgs.msg")
    std_msgs_msg.Int32 = type("Int32", (), {})

    for name, module in {
        "rclpy": fake,
        "rclpy.node": node_module,
        "rclpy.executors": executors,
        "rclpy.context": context_module,
        "rclpy.qos": qos,
        "sensor_msgs": sensor_msgs,
        "sensor_msgs.msg": sensor_msgs_msg,
        "std_msgs": std_msgs,
        "std_msgs.msg": std_msgs_msg,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    fake.node_module = node_module
    return fake


def reset_fake_node():
    FakeNode.instances = []


def test_single_rclpy_init_and_shared_node(monkeypatch):
    fake = install_fake_ros(monkeypatch)
    reset_fake_node()
    runtime = RosRuntime({"executor_threads": 2}, node_name="audio_service")
    assert runtime.start() is True
    assert runtime.start() is True
    assert fake.init_calls == 1
    node = runtime.node
    assert node.name == "audio_service"

    capture = RosCaptureSubscriber(
        {"topic": "/audio/capture_pcm", "encoding": "pcm_16000_16_1"},
        on_frame=lambda frame: None,
        runtime=runtime,
    )
    assert capture.start() is True
    publisher = RosPcmPublisher(
        {
            "node_name": "audio_service_publisher",
            "speaker_topic": "/audio/speaker_pcm",
            "speaker_status_topic": "/audio/speaker_pcm_status",
        },
        runtime=runtime,
        capture_stats=lambda: {"online": True, "input_rate_hz": 15.6},
    )
    assert publisher.start() is True

    # 输入订阅与输出发布共享同一个 Node，且只初始化一次 rclpy
    assert fake.init_calls == 1
    assert FakeNode.instances == [node]
    assert [item.topic for item in node.subscriptions if item.topic == "/audio/capture_pcm"] == [
        "/audio/capture_pcm"
    ]
    assert [item[0] for item in node.publishers] == ["/audio/speaker_pcm"]
    assert publisher.status()["capture"]["rate_hz"] == 15.6
    assert publisher.status()["capture"]["active"] is True

    # 停止发布器不应销毁共享节点或关闭 rclpy
    publisher.stop()
    assert node.destroyed is False
    assert fake.shutdown_calls == 0

    capture.stop()
    assert node.subscriptions == []
    runtime.stop()
    assert node.destroyed is True
    assert fake.shutdown_calls == 1
    assert runtime.available is False


def test_private_domain_uses_dedicated_context(monkeypatch):
    fake = install_fake_ros(monkeypatch)
    reset_fake_node()
    runtime = RosRuntime({"executor_threads": 1, "domain_id": 43}, node_name="media_bridge")
    assert runtime.start() is True
    context = runtime.node.context
    assert fake.init_calls == 1
    assert context.domain_id == 43
    assert runtime.status()["domain_id"] == 43
    runtime.stop()
    assert context.shutdown_calls == 1
    assert fake.shutdown_calls == 0


def test_publisher_standalone_owns_node(monkeypatch):
    fake = install_fake_ros(monkeypatch)
    reset_fake_node()
    publisher = RosPcmPublisher(
        {
            "node_name": "standalone_publisher",
            "speaker_topic": "/audio/speaker_pcm",
            "speaker_status_topic": "/audio/speaker_pcm_status",
        }
    )
    assert publisher.start() is True
    node = FakeNode.instances[0]
    assert fake.init_calls == 1
    deadline = time.monotonic() + 0.5
    while not fake.spun_nodes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.spun_nodes == [node]

    publisher.stop()
    assert node.destroyed is True
    assert fake.shutdown_calls == 1


def test_capture_reports_publisher_count_and_rates(monkeypatch):
    install_fake_ros(monkeypatch)
    reset_fake_node()
    runtime = RosRuntime({}, node_name="audio_service")
    assert runtime.start() is True
    capture = RosCaptureSubscriber(
        {"topic": "/audio/capture_pcm", "encoding": "pcm_16000_16_1"},
        on_frame=lambda frame: None,
        runtime=runtime,
    )
    assert capture.start() is True
    status = capture.status()
    assert status["publishers"] == 1
    assert status["qos"] == {"reliability": "reliable", "depth": 100, "history": "keep_last"}
    assert status["subscribed"] is True
    assert status["online"] is False  # 尚未收到真实帧
    capture.stop()
    runtime.stop()


def test_runtime_start_failure_is_reported(monkeypatch):
    class BrokenNode:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("dds unavailable")

    fake = install_fake_ros(monkeypatch)
    monkeypatch.setattr(fake.node_module, "Node", BrokenNode)
    runtime = RosRuntime({}, node_name="audio_service")
    assert runtime.start() is False
    assert "dds unavailable" in (runtime.error or "")
    assert runtime.available is False
