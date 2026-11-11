import logging
import threading
import time


LOG = logging.getLogger("vision_service.ros")


class VisionRosRuntime:
    def __init__(self, ros_config, processor_config, on_result):
        self.ros_config = dict(ros_config)
        self.processor_config = dict(processor_config)
        self.on_result = on_result
        self.available = False
        self.last_error = None
        self.node = None
        self.executor = None
        self.publisher = None
        self.subscription = None
        self.thread = None
        self.rclpy = None
        self._owns_context = False

    def start(self):
        try:
            import rclpy
            from rclpy.executors import MultiThreadedExecutor
            from rclpy.node import Node
            from std_msgs.msg import Int32, String

            self.rclpy = rclpy
            if not rclpy.ok():
                rclpy.init(args=None)
                self._owns_context = True
            self.node = Node(self.ros_config.get("node_name", "vision_service"))
            self.publisher = self.node.create_publisher(
                Int32, self.processor_config["enable_topic"], 10
            )

            def callback(message):
                now_wall = time.time()
                now_mono = time.monotonic()
                self.on_result(message.data, now_wall, now_mono)

            self.subscription = self.node.create_subscription(
                String, self.processor_config["result_topic"], callback, 10
            )
            threads = max(1, int(self.ros_config.get("executor_threads", 2)))
            self.executor = MultiThreadedExecutor(num_threads=threads)
            self.executor.add_node(self.node)
            self.thread = threading.Thread(
                target=self.executor.spin, name="vision-ros", daemon=True
            )
            self.thread.start()
            self.available = True
            self.last_error = None
            LOG.info("ROS runtime started: result=%s enable=%s",
                     self.processor_config["result_topic"],
                     self.processor_config["enable_topic"])
        except Exception as error:
            self.last_error = str(error)
            LOG.exception("ROS runtime failed to start")
            self.stop()
            raise

    def set_detection_enabled(self, enabled):
        if not self.available or self.publisher is None:
            raise RuntimeError("ROS runtime is unavailable")
        from std_msgs.msg import Int32

        message = Int32()
        message.data = 1 if enabled else 0
        repeats = max(1, int(self.processor_config.get("enable_publish_repeats", 3)))
        interval = max(0.0, float(
            self.processor_config.get("enable_publish_interval_seconds", 0.1)
        ))
        for index in range(repeats):
            self.publisher.publish(message)
            if index + 1 < repeats and interval:
                time.sleep(interval)
        LOG.info("vendor multitask detection flag=%d repeats=%d", message.data, repeats)

    def status(self):
        publishers = None
        subscribers = None
        if self.node is not None:
            try:
                publishers = self.node.count_publishers(self.processor_config["result_topic"])
                subscribers = self.node.count_subscribers(self.processor_config["enable_topic"])
            except Exception:
                pass
        return {
            "available": self.available,
            "node": self.ros_config.get("node_name", "vision_service"),
            "result_topic": self.processor_config.get("result_topic"),
            "enable_topic": self.processor_config.get("enable_topic"),
            "result_publishers": publishers,
            "enable_subscribers": subscribers,
            "last_error": self.last_error,
        }

    def stop(self):
        self.available = False
        try:
            if self.executor is not None:
                self.executor.shutdown(timeout_sec=2.0)
        except Exception:
            LOG.exception("executor shutdown failed")
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        try:
            if self.node is not None:
                self.node.destroy_node()
        except Exception:
            LOG.exception("node shutdown failed")
        try:
            if self.rclpy is not None and self._owns_context and self.rclpy.ok():
                self.rclpy.shutdown()
        except Exception:
            LOG.exception("rclpy shutdown failed")
        self.node = None
        self.executor = None
        self.publisher = None
        self.subscription = None
        self.thread = None
