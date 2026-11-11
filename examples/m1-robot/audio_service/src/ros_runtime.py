"""进程内唯一的 ROS 2 运行时。

TTS 输出（/audio/speaker_pcm 发布）与音频输入（/audio/capture_pcm 订阅）共享
同一个 rclpy Context、Node 与 Executor，保证：

* 服务生命周期内只执行一次 rclpy.init / rclpy.shutdown；
* 输入回调与输出发布可同时工作，互不阻塞（MultiThreadedExecutor）；
* 关闭时按 订阅 → 发布器 → Executor → Node → Context 的顺序销毁。

本模块不导入 rclpy，缺少 ROS 环境时 start() 返回 False，服务其余部分仍可运行。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

LOG = logging.getLogger(__name__)


class RosRuntime:
    def __init__(self, config: Optional[Dict[str, Any]] = None, node_name: Optional[str] = None):
        config = config or {}
        self.node_name = str(node_name or config.get("runtime_node_name", "audio_service"))
        self.executor_threads = max(1, int(config.get("executor_threads", 2)))
        raw_domain_id = config.get("domain_id")
        self.domain_id = int(raw_domain_id) if raw_domain_id is not None else None

        self._node = None
        self._executor = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._owns_context = False
        self._context = None
        self._error: Optional[str] = None
        self._lock = threading.RLock()

    # ---- 状态 -----------------------------------------------------
    @property
    def node(self):
        return self._node

    @property
    def available(self) -> bool:
        return bool(self._started and self._node is not None)

    @property
    def error(self) -> Optional[str]:
        return self._error

    # ---- 生命周期 -------------------------------------------------
    def start(self) -> bool:
        with self._lock:
            if self._started:
                return True
            try:
                import rclpy
                from rclpy.executors import MultiThreadedExecutor
                from rclpy.node import Node
            except Exception as exc:  # pragma: no cover - 机器人上 rclpy 总是存在
                self._error = f"{type(exc).__name__}: {exc}"
                LOG.error("ROS 2 不可用：%s", self._error)
                return False

            try:
                if self.domain_id is not None:
                    from rclpy.context import Context

                    context = Context()
                    rclpy.init(args=None, context=context, domain_id=self.domain_id)
                    self._owns_context = True
                    self._context = context
                    node = Node(self.node_name, context=context, start_parameter_services=False)
                    executor = MultiThreadedExecutor(
                        num_threads=self.executor_threads, context=context
                    )
                else:
                    if not rclpy.ok():
                        try:
                            rclpy.init(args=None)
                            self._owns_context = True
                        except Exception as exc:
                            # 已被其它组件初始化：继续复用现有 context
                            LOG.warning("rclpy.init 提示：%s", exc)
                    node = Node(self.node_name, start_parameter_services=False)
                    executor = MultiThreadedExecutor(num_threads=self.executor_threads)
                executor.add_node(node)
                thread = threading.Thread(
                    target=self._spin, args=(executor,), name="ros-runtime", daemon=True
                )
                thread.start()
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                LOG.error("ROS 2 运行时初始化失败：%s", self._error)
                self._owns_context = False
                return False

            self._node = node
            self._executor = executor
            self._thread = thread
            self._started = True
            self._error = None
            LOG.info(
                "ROS 运行时就绪 node=%s domain=%s threads=%d owns_context=%s",
                self.node_name,
                self.domain_id if self.domain_id is not None else "default",
                self.executor_threads,
                self._owns_context,
            )
            return True

    def _spin(self, executor) -> None:
        try:
            executor.spin()
        except Exception:  # pragma: no cover - 关闭过程或底层 DDS 异常
            LOG.exception("ROS Executor 退出")

    def stop(self) -> None:
        with self._lock:
            executor = self._executor
            node = self._node
            thread = self._thread
            self._executor = None
            self._node = None
            self._thread = None
            self._started = False

        if executor is not None:
            try:
                executor.shutdown(timeout_sec=2.0)
            except Exception:  # pragma: no cover
                LOG.debug("Executor 关闭异常", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        if node is not None:
            try:
                node.destroy_node()
            except Exception:  # pragma: no cover
                LOG.debug("Node 销毁异常", exc_info=True)
        context = self._context
        self._context = None
        if self._owns_context:
            try:
                if context is not None:
                    context.try_shutdown()
                else:
                    import rclpy

                    rclpy.try_shutdown()
            except Exception:  # pragma: no cover
                LOG.debug("rclpy 关闭异常", exc_info=True)
            self._owns_context = False
        LOG.info("ROS 运行时已关闭")

    # ---- 便捷封装 -------------------------------------------------
    def create_publisher(self, message_type, topic: str, qos):
        if self._node is None:
            raise RuntimeError("ROS 运行时未启动")
        return self._node.create_publisher(message_type, topic, qos)

    def create_subscription(self, message_type, topic: str, callback, qos):
        if self._node is None:
            raise RuntimeError("ROS 运行时未启动")
        return self._node.create_subscription(message_type, topic, callback, qos)

    def count_publishers(self, topic: str) -> Optional[int]:
        if self._node is None:
            return None
        try:
            return int(self._node.count_publishers(topic))
        except Exception:  # pragma: no cover
            return None

    def count_subscribers(self, topic: str) -> Optional[int]:
        if self._node is None:
            return None
        try:
            return int(self._node.count_subscribers(topic))
        except Exception:  # pragma: no cover
            return None

    def now_stamp(self):
        if self._node is None:
            return None
        return self._node.get_clock().now().to_msg()

    def status(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "node": self.node_name,
            "domain_id": self.domain_id,
            "executor_threads": self.executor_threads,
            "owns_context": self._owns_context,
            "error": self._error,
        }
