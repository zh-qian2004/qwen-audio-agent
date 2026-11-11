import json
import logging
import math
import threading
import time


LOG = logging.getLogger("dashboard.ros")


def _euler_from_quaternion(q):
    sinr_cosp = 2.0 * (q.w * q.x + q.y * q.z)
    cosr_cosp = 1.0 - 2.0 * (q.x * q.x + q.y * q.y)
    roll = math.atan2(sinr_cosp, cosr_cosp)
    sinp = 2.0 * (q.w * q.y - q.z * q.x)
    pitch = math.copysign(math.pi / 2.0, sinp) if abs(sinp) >= 1.0 else math.asin(sinp)
    siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
    cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return tuple(math.degrees(value) for value in (roll, pitch, yaw))


class ROSCollector:
    def __init__(self, store):
        self.store = store
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="ros2-readonly", daemon=True)
        self.last_joint_write = 0.0
        self.last_imu_write = 0.0
        self.node = None
        self.business_lock = threading.Lock()
        self.business_publisher = None
        self.business_message_type = None
        self.frame_lock = threading.Lock()
        self.latest_jpeg = None
        self.latest_jpeg_mono = None
        self.last_frame_store = 0.0

    def get_latest_jpeg(self):
        with self.frame_lock:
            return self.latest_jpeg, self.latest_jpeg_mono

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def publish_business_request(self, payload):
        """Publish a high-level vendor instruction through the business layer."""
        with self.business_lock:
            publisher = self.business_publisher
            message_type = self.business_message_type
        if publisher is None or message_type is None:
            raise RuntimeError("ROS business publisher is not ready")
        message = message_type()
        message.data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        publisher.publish(message)
        return message.data

    def run(self):
        rclpy = None
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.node import Node
            from rclpy.qos import QoSProfile, ReliabilityPolicy
            from rclpy.signals import SignalHandlerOptions
            from hardware.msg import Bat, HeadPosition, KinematicState, VehicleFeedback
            from plan.msg import BipedMotionCmd
            from sensor_msgs.msg import CameraInfo, CompressedImage, Imu, LaserScan
            from std_msgs.msg import Int16MultiArray, Int32MultiArray, String, UInt8, UInt8MultiArray, UInt32

            rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
            self.node = Node("zeroth_dashboard")
            node = self.node
            executor = SingleThreadedExecutor()
            executor.add_node(node)
            best_effort = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
            reliable = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
            with self.business_lock:
                self.business_message_type = String
                self.business_publisher = node.create_publisher(
                    String, "/BusinessAppReq_topic", reliable)

            def subscribe(message_type, topic, callback, qos=best_effort):
                return node.create_subscription(message_type, topic, callback, qos)

            def on_joints(message):
                self.store.note_topic("joints")
                now = time.monotonic()
                if now - self.last_joint_write >= 0.1:
                    self.last_joint_write = now
                    self.store.update_joints(message.joint_angles.data, message.joint_velocities.data, note=False)

            def on_imu(message):
                self.store.note_topic("imu")
                now = time.monotonic()
                if now - self.last_imu_write < 0.1:
                    return
                self.last_imu_write = now
                roll, pitch, yaw = _euler_from_quaternion(message.orientation)
                vector = lambda value: {"x": float(value.x), "y": float(value.y), "z": float(value.z)}
                self.store.update_imu(
                    roll, pitch, yaw, vector(message.angular_velocity),
                    vector(message.linear_acceleration), message.header.frame_id, note=False,
                )

            def on_battery(message):
                self.store.update_battery({
                    "power": int(message.power),
                    "voltage": float(message.bat_vol) / 1000.0,
                    "current_ma": int(message.bat_cur),
                    "temperature_raw": int(message.bat_temp),
                    "plate_voltage": float(message.plate_vol) / 1000.0,
                    "on_charger_raw": int(message.on_charger),
                    "is_charging": bool(message.is_charging),
                    "charge_full": bool(message.charge_full),
                    "charge_error": bool(message.charge_error),
                })

            def on_motion_feedback(message):
                def pick(*names):
                    for name in names:
                        value = getattr(message, name, None)
                        if value is not None:
                            try:
                                return int(value)
                            except (TypeError, ValueError):
                                return None
                    return None

                self.store.update_motion_feedback({
                    "control_mode": pick("control_mode", "ctrl_mode"),
                    "gait_type": pick("gait_type"),
                    "linear_velocity_x": pick("linear_velocity_x", "vx"),
                    "angular_rate_z": pick("angular_rate_z", "angular_velocity_z"),
                    "one_time_turn_angle": pick("one_time_turn_angle"),
                    "leg_height_setting": pick("leg_height_setting_val", "leg_height_setting"),
                })

            def lidar_summary(message):
                ranges = message.ranges
                valid = sum(1 for value in ranges if math.isfinite(value) and value > 0)
                self.store.update_sensor("lidar", {
                    "frame_id": message.header.frame_id,
                    "points": len(ranges), "valid_points": valid,
                    "range_min": float(message.range_min), "range_max": float(message.range_max),
                })

            def camera_summary(key):
                def callback(message):
                    self.store.update_sensor(key, {
                        "frame_id": message.header.frame_id,
                        "width": int(message.width), "height": int(message.height),
                        "distortion_model": message.distortion_model,
                    })
                return callback

            def on_camera_frame(message):
                now = time.monotonic()
                if now - self.last_frame_store < 0.5:
                    return
                self.last_frame_store = now
                with self.frame_lock:
                    self.latest_jpeg = bytes(message.data)
                    self.latest_jpeg_mono = now

            def on_audio_keyword(message):
                self.store.note_topic("audio_keyword")
                self.store.update_sensor(
                    "audio", {"last_event": list(message.data)}, note=False)

            subscriptions = [
                subscribe(KinematicState, "/HardwareKinematic_topic", on_joints),
                subscribe(Imu, "/HardwareImuStd_topic", on_imu),
                subscribe(Bat, "/HardwareBat_topic", on_battery),
                subscribe(HeadPosition, "/HardwareHeadPosition_topic",
                          lambda message: self.store.update_head(message.data)),
                subscribe(VehicleFeedback, "/HardwareVehicleFeedback_topic",
                          lambda message: self.store.update_vehicle(message.data)),
                subscribe(BipedMotionCmd, "/ControlBipedMotionFeedback_topic", on_motion_feedback),
                subscribe(UInt32, "/HardwareSelfFall_topic",
                          lambda message: self.store.update_fall(message.data)),
                subscribe(Int16MultiArray, "/HardwareMotorCurrent_topic",
                          lambda message: self.store.update_motor_feedback("motor_current", message.data)),
                subscribe(Int16MultiArray, "/HardwareMotorLoad_topic",
                          lambda message: self.store.update_motor_feedback("motor_load", message.data)),
                subscribe(UInt8MultiArray, "/HardwareMotorTemp_topic",
                          lambda message: self.store.update_motor_feedback("motor_temp", message.data)),
                subscribe(UInt8MultiArray, "/HardwareMotorErr_topic",
                          lambda message: self.store.update_motor_feedback("motor_error", message.data)),
                subscribe(LaserScan, "/HardwareLidar_topic", lidar_summary, reliable),
                subscribe(CameraInfo, "/HardwareItof/camera_info", camera_summary("itof"), reliable),
                subscribe(CameraInfo, "/camera/camera_info", camera_summary("camera"), reliable),
                subscribe(CompressedImage, "/camera/image_jpeg", on_camera_frame),
                # /audio/capture_pcm 不再由 Dashboard 订阅：统一由 audio_service Gateway 消费
                subscribe(Int32MultiArray, "/audio/kw_detect", on_audio_keyword, reliable),
                subscribe(String, "/BusinessAppRpt_topic",
                          lambda message: self.store.update_business_report(message.data), reliable),
                subscribe(UInt8, "/ControlGoChargeDone_topic",
                          lambda message: self.store.update_go_charge_result(message.data)),
            ]
            # Keep the subscription handles alive for the lifetime of the node.
            _ = subscriptions
            self.store.set_ros_status("online")
            LOG.info("ROS 2 collector started with %d subscriptions", len(subscriptions))
            next_graph = 0.0
            while not self.stop_event.is_set() and rclpy.ok():
                executor.spin_once(timeout_sec=0.2)
                now = time.monotonic()
                if now >= next_graph:
                    next_graph = now + 2.0
                    nodes = sorted(
                        (namespace.rstrip("/") + "/" + name).replace("//", "/")
                        for name, namespace in node.get_node_names_and_namespaces()
                    )
                    topics = {name: types for name, types in node.get_topic_names_and_types()}
                    presence_topics = {item["presence_topic"] for item in self.store.config["sensors"].values()}
                    presence_topics.update(item["name"] for item in self.store.config["topics"].values())
                    publisher_counts = {topic: node.count_publishers(topic) for topic in presence_topics}
                    self.store.update_graph(nodes, topics, publisher_counts)
            executor.shutdown()
        except Exception as error:
            LOG.exception("ROS 2 collector stopped")
            self.store.set_ros_status("error", str(error))
        finally:
            with self.business_lock:
                self.business_publisher = None
                self.business_message_type = None
            if self.node is not None:
                try:
                    self.node.destroy_node()
                except Exception:
                    LOG.exception("Failed to destroy ROS node")
            try:
                if rclpy is not None and rclpy.ok():
                    rclpy.shutdown()
            except Exception:
                LOG.exception("Failed to shut down rclpy")
            if self.store.snapshot()["ros"]["status"] != "error":
                self.store.set_ros_status("offline")
