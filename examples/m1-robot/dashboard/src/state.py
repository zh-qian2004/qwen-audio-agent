import copy
import datetime as dt
import json
import re
import threading
import time
from collections import deque
from pathlib import Path


BATTERY_LOG_PATTERN = re.compile(
    r"\[I\]\[(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\]"
    r".*battery changed to:\s*(?P<power>\d+)"
)


class TopicStats:
    def __init__(self, name, message_type, stale_after):
        self.name = name
        self.type = message_type
        self.stale_after = stale_after
        self.count = 0
        self.last_mono = None
        self.last_wall = None
        self.events = deque()

    def note(self, now_mono, now_wall):
        self.count += 1
        self.last_mono = now_mono
        self.last_wall = now_wall
        self.events.append(now_mono)
        self._trim(now_mono)

    def _trim(self, now_mono):
        while self.events and now_mono - self.events[0] > 5.0:
            self.events.popleft()

    def snapshot(self, now_mono):
        self._trim(now_mono)
        elapsed = self.events[-1] - self.events[0] if len(self.events) > 1 else 0.0
        rate = (len(self.events) - 1) / elapsed if elapsed > 0.001 else 0.0
        age = now_mono - self.last_mono if self.last_mono is not None else None
        if age is None:
            status = "no_data"
        elif age > self.stale_after:
            status = "stale"
        else:
            status = "online"
        return {
            "name": self.name,
            "type": self.type,
            "count": self.count,
            "rate_hz": round(rate, 2),
            "age_seconds": round(age, 3) if age is not None else None,
            "last_update": self.last_wall,
            "status": status,
            "stale_after": self.stale_after,
        }


class StateStore:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.started_wall = time.time()
        self.started_mono = time.monotonic()
        self.topics = {
            key: TopicStats(value["name"], value["type"], value["stale_after"])
            for key, value in config["topics"].items()
        }
        maxlen = config["history_seconds"] * 12
        self.battery_history_seconds = int(config.get("battery_history_seconds", 6 * 60 * 60))
        self.battery_sample_seconds = float(config.get("battery_history_sample_seconds", 15))
        battery_maxlen = max(120, int(self.battery_history_seconds / max(1, self.battery_sample_seconds)) + 240)
        self.history = {
            "joints": deque(maxlen=maxlen),
            "imu": deque(maxlen=maxlen),
            "battery": deque(maxlen=battery_maxlen),
        }
        self._last_battery_history_t = None
        self._last_battery_signature = None
        self.data = {
            "ros": {
                "connected": False,
                "status": "starting",
                "error": None,
                "node_count": 0,
                "topic_count": 0,
                "nodes": [],
                "last_update": None,
            },
            "graph_topics": {},
            "joints": [
                {**joint, "angle": None, "velocity": None, "current": None,
                 "load": None, "temperature": None, "error": None}
                for joint in config["joints"]
            ],
            "head": {"yaw": None, "pitch": None, "yaw_speed_raw": None, "pitch_speed_raw": None},
            "imu": {
                "roll": None, "pitch": None, "yaw": None,
                "angular_velocity": None, "linear_acceleration": None,
                "frame_id": None,
            },
            "battery": {},
            "vehicle": {},
            "business": {
                "is_on_car": None,
                "is_on_charger": None,
                "is_charging": None,
                "work_mode": None,
                "stand_status": None,
                "last_report_at": None,
                "last_event": None,
            },
            "go_charge_result": None,
            "motion": {
                "control_mode": None,
                "gait_type": None,
                "linear_velocity_x": None,
                "angular_rate_z": None,
                "one_time_turn_angle": None,
                "leg_height_setting": None,
                "fall_state": None,
                "last_update": None,
            },
            "sensors": {
                key: {"label": value["label"], "summary": {}, "publisher_count": 0,
                      "topic_publisher_count": 0}
                for key, value in config["sensors"].items()
            },
            "system": {},
        }
        self._load_battery_history()

    def _load_battery_history(self):
        """Seed the long battery curve from the vendor status log when available."""
        log_path = self.config.get("battery_history_log")
        if not log_path:
            return
        try:
            path = Path(log_path)
            if not path.is_file():
                return
            size = path.stat().st_size
            with path.open("rb") as stream:
                stream.seek(max(0, size - 4 * 1024 * 1024))
                raw = stream.read().decode("utf-8", errors="ignore")
            cutoff = time.time() - self.battery_history_seconds
            for match in BATTERY_LOG_PATTERN.finditer(raw):
                timestamp = dt.datetime.strptime(
                    match.group("timestamp"), "%Y-%m-%d %H:%M:%S.%f"
                ).timestamp()
                if timestamp < cutoff:
                    continue
                self.history["battery"].append({
                    "t": timestamp,
                    "power": int(match.group("power")),
                    "voltage": None,
                    "is_charging": None,
                    "current_ma": None,
                    "source": "business_log",
                })
            if self.history["battery"]:
                last = self.history["battery"][-1]
                self._last_battery_history_t = last["t"]
                self._last_battery_signature = (last["power"], None)
        except (OSError, ValueError):
            # Battery telemetry remains available even if this optional bootstrap fails.
            return

    def note_topic(self, key):
        with self.lock:
            self.topics[key].note(time.monotonic(), time.time())
            last = self.topics[key].last_wall
            if self.data["ros"]["last_update"] is None or last > self.data["ros"]["last_update"]:
                self.data["ros"]["last_update"] = last

    def set_ros_status(self, status, error=None):
        with self.lock:
            self.data["ros"]["connected"] = status == "online"
            self.data["ros"]["status"] = status
            self.data["ros"]["error"] = error

    def update_graph(self, nodes, topics, publisher_counts):
        with self.lock:
            self.data["ros"]["nodes"] = nodes
            self.data["ros"]["node_count"] = len(nodes)
            self.data["ros"]["topic_count"] = len(topics)
            self.data["graph_topics"] = topics
            for key, sensor in self.data["sensors"].items():
                presence = self.config["sensors"][key]["presence_topic"]
                self.data["sensors"][key]["publisher_count"] = publisher_counts.get(presence, 0)
                monitored = self.config["topics"][self.config["sensors"][key]["topic_key"]]["name"]
                self.data["sensors"][key]["topic_publisher_count"] = publisher_counts.get(monitored, 0)

    def _append_history(self, key, item, history_seconds=None):
        history = self.history[key]
        history.append(item)
        cutoff = item["t"] - (history_seconds or self.config["history_seconds"])
        while history and history[0]["t"] < cutoff:
            history.popleft()

    def update_joints(self, angles, velocities, note=True):
        with self.lock:
            if note:
                self.note_topic("joints")
            for index, joint in enumerate(self.data["joints"]):
                if index < len(angles):
                    joint["angle"] = float(angles[index])
                if index < len(velocities):
                    joint["velocity"] = int(velocities[index])
            self._append_history("joints", {
                "t": time.time(),
                "angles": [joint["angle"] for joint in self.data["joints"]],
                "velocities": [joint["velocity"] for joint in self.data["joints"]],
            })

    def update_motor_feedback(self, key, values):
        field = {"motor_current": "current", "motor_load": "load",
                 "motor_temp": "temperature", "motor_error": "error"}[key]
        with self.lock:
            self.note_topic(key)
            for index, joint in enumerate(self.data["joints"]):
                if index < len(values):
                    joint[field] = int(values[index])

    def update_imu(self, roll, pitch, yaw, angular_velocity, linear_acceleration, frame_id, note=True):
        with self.lock:
            if note:
                self.note_topic("imu")
            self.data["imu"] = {
                "roll": roll, "pitch": pitch, "yaw": yaw,
                "angular_velocity": angular_velocity,
                "linear_acceleration": linear_acceleration,
                "frame_id": frame_id,
            }
            self._append_history("imu", {"t": time.time(), "roll": roll, "pitch": pitch, "yaw": yaw})

    def update_battery(self, message):
        with self.lock:
            self.note_topic("battery")
            self.data["battery"] = message
            now = time.time()
            signature = (message.get("power"), message.get("is_charging"))
            due = self._last_battery_history_t is None or now - self._last_battery_history_t >= self.battery_sample_seconds
            if due or signature != self._last_battery_signature:
                self._append_history("battery", {
                    "t": now,
                    "power": message.get("power"),
                    "voltage": message.get("voltage"),
                    "is_charging": message.get("is_charging"),
                    "current_ma": message.get("current_ma"),
                    "source": "ros",
                }, self.battery_history_seconds)
                self._last_battery_history_t = now
                self._last_battery_signature = signature

    def update_business_report(self, raw):
        """Consume the vendor's authoritative APP status/event stream."""
        try:
            report = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(report, dict):
            return False

        report_type = report.get("type")
        payload = report.get("payload") or {}
        if not isinstance(payload, dict):
            return False
        now = time.time()
        with self.lock:
            business = self.data["business"]
            business["last_report_at"] = now
            if report_type == "system_status":
                values = payload.get("data") or {}
                if not isinstance(values, dict):
                    return False
                for key in ("is_on_car", "is_on_charger", "is_charging", "work_mode", "stand_status"):
                    if key in values:
                        business[key] = values[key]
                return True
            if report_type in ("get_on_off_car_status", "on_off_charger_status"):
                business["last_event"] = {
                    "type": report_type,
                    "action": payload.get("action"),
                    "status": payload.get("status"),
                    "reason": payload.get("reason"),
                    "error_code": payload.get("error_code"),
                    "received_at": now,
                }
                return True
        return False

    def update_go_charge_result(self, value):
        """Retain the latest charging-dock controller completion code with its age."""
        with self.lock:
            self.note_topic("go_charge_done")
            self.data["go_charge_result"] = {
                "value": int(value),
                "received_at": time.time(),
            }

    def battery_status(self):
        """Return charge-rate and ETA estimates derived from the current rising SOC segment."""
        with self.lock:
            battery = copy.deepcopy(self.data["battery"])
            points = [copy.deepcopy(point) for point in self.history["battery"]
                      if isinstance(point.get("power"), (int, float))]

        result = {
            "history_seconds": self.battery_history_seconds,
            "sample_seconds": self.battery_sample_seconds,
            "charging_rate_percent_per_hour": None,
            "eta_30_minutes": None,
            "eta_full_minutes": None,
            "segment_start": None,
            "segment_end": None,
            "soc_delta": None,
            "duration_minutes": None,
            "sample_count": 0,
        }
        if not battery.get("is_charging") or len(points) < 2:
            return result

        # Collapse repeated SOC samples and retain the first timestamp of each step.
        changes = []
        for point in points:
            power = float(point["power"])
            if not changes or power != changes[-1]["power"]:
                changes.append({"t": float(point["t"]), "power": power})
        if len(changes) < 2:
            return result

        # Walk back through the latest monotonically rising segment. A forward SOC
        # drop marks a new charging session (or an interrupted/loaded calibration).
        start_index = len(changes) - 1
        while start_index > 0:
            previous = changes[start_index - 1]
            current = changes[start_index]
            if previous["power"] > current["power"] or current["t"] - previous["t"] > 30 * 60:
                break
            start_index -= 1
        segment = changes[start_index:]
        first, last = segment[0], segment[-1]
        elapsed = last["t"] - first["t"]
        delta = last["power"] - first["power"]
        if elapsed < 60 or delta < 1:
            return result
        rate = delta / elapsed * 3600.0
        if not 0 < rate <= 100:
            return result

        current_power = float(battery.get("power", last["power"]))
        result.update({
            "charging_rate_percent_per_hour": round(rate, 2),
            "eta_30_minutes": round(max(0.0, 30.0 - current_power) / rate * 60.0, 1),
            "eta_full_minutes": round(max(0.0, 100.0 - current_power) / rate * 60.0, 1),
            "segment_start": first["t"],
            "segment_end": last["t"],
            "soc_delta": round(delta, 1),
            "duration_minutes": round(elapsed / 60.0, 1),
            "sample_count": len(segment),
        })
        return result

    def update_head(self, data):
        with self.lock:
            self.note_topic("head")
            self.data["head"] = {
                "yaw": float(data[0]) / 100.0 if len(data) > 0 else None,
                "yaw_speed_raw": int(data[1]) if len(data) > 1 else None,
                "pitch": float(data[2]) / 100.0 if len(data) > 2 else None,
                "pitch_speed_raw": int(data[3]) if len(data) > 3 else None,
            }

    def update_vehicle(self, data):
        names = ["reserved", "left_mileage_raw", "right_mileage_raw", "left_speed_raw",
                 "right_speed_raw", "left_fault", "right_fault", "comm_status",
                 "left_current_raw", "right_current_raw"]
        with self.lock:
            self.note_topic("vehicle")
            self.data["vehicle"] = {name: int(data[i]) if i < len(data) else None
                                    for i, name in enumerate(names)}

    def vehicle_status(self):
        """Return a stable, read-only view of the cart feedback topic.

        VehicleFeedback exposes wheel and communication telemetry but no documented
        field that says whether the robot is physically mounted on the cart. Keep
        that distinction explicit so callers do not mistake a live link or zero
        wheel speed for an occupancy signal.
        """
        now = time.monotonic()
        with self.lock:
            raw = copy.deepcopy(self.data["vehicle"])
            business = copy.deepcopy(self.data["business"])
            topic = self.topics["vehicle"].snapshot(now)

        left_speed = raw.get("left_speed_raw")
        right_speed = raw.get("right_speed_raw")
        speeds_known = left_speed is not None and right_speed is not None
        wheel_moving = ((left_speed != 0) or (right_speed != 0)) if speeds_known else None
        left_fault = raw.get("left_fault")
        right_fault = raw.get("right_fault")
        faults_known = left_fault is not None and right_fault is not None

        mount_value = business.get("is_on_car")
        mount_known = mount_value in (0, 1, False, True)
        return {
            "available": bool(raw),
            "status": topic["status"],
            "topic": topic,
            "communication": {
                "feedback_online": topic["status"] == "online",
                "raw": raw.get("comm_status"),
            },
            "mount": {
                "state": "on_vehicle" if bool(mount_value) else "off_vehicle" if mount_known else "unknown",
                "known": mount_known,
                "source": "/BusinessAppRpt_topic" if mount_known else "unavailable",
                "reason": "厂商业务状态 is_on_car" if mount_known else "尚未收到厂商业务状态 is_on_car",
            },
            "wheel_motion": {
                "state": "moving" if wheel_moving else "stopped" if wheel_moving is False else "unknown",
                "moving": wheel_moving,
                "left_speed_raw": left_speed,
                "right_speed_raw": right_speed,
            },
            "odometry": {
                "left_mileage_raw": raw.get("left_mileage_raw"),
                "right_mileage_raw": raw.get("right_mileage_raw"),
            },
            "current": {
                "left_raw": raw.get("left_current_raw"),
                "right_raw": raw.get("right_current_raw"),
            },
            "faults": {
                "active": ((left_fault != 0) or (right_fault != 0)) if faults_known else None,
                "left_raw": left_fault,
                "right_raw": right_fault,
            },
            "reserved_raw": raw.get("reserved"),
            "raw": raw,
        }

    def update_motion_feedback(self, values):
        with self.lock:
            self.note_topic("motion_feedback")
            for key in ("control_mode", "gait_type", "linear_velocity_x", "angular_rate_z",
                        "one_time_turn_angle", "leg_height_setting"):
                if key in values:
                    self.data["motion"][key] = values[key]
            self.data["motion"]["last_update"] = time.time()

    def update_fall(self, fall_state):
        with self.lock:
            self.note_topic("self_fall")
            self.data["motion"]["fall_state"] = int(fall_state)

    def update_sensor(self, key, summary, note=True):
        with self.lock:
            if note:
                self.note_topic(self.config["sensors"][key]["topic_key"])
            self.data["sensors"][key]["summary"].update(summary)

    def update_system(self, system):
        with self.lock:
            self.data["system"] = system

    def motion_observation(self):
        """Compact snapshot consumed by the motion state machine."""
        now = time.monotonic()
        with self.lock:
            motion = copy.deepcopy(self.data["motion"])
            vehicle = copy.deepcopy(self.data["vehicle"])
            battery = copy.deepcopy(self.data["battery"])
            business = copy.deepcopy(self.data["business"])
            go_charge_result = copy.deepcopy(self.data["go_charge_result"])
            head = copy.deepcopy(self.data["head"])
            joints = [joint.get("angle") for joint in self.data["joints"]]
            ages = {
                "motion_feedback": self.topics["motion_feedback"].snapshot(now)["age_seconds"],
                "self_fall": self.topics["self_fall"].snapshot(now)["age_seconds"],
                "vehicle": self.topics["vehicle"].snapshot(now)["age_seconds"],
                "head": self.topics["head"].snapshot(now)["age_seconds"],
                "kinematic": self.topics["joints"].snapshot(now)["age_seconds"],
            }
        return {
            "control_mode": motion.get("control_mode"),
            "gait_type": motion.get("gait_type"),
            "linear_velocity_x": motion.get("linear_velocity_x"),
            "angular_rate_z": motion.get("angular_rate_z"),
            "one_time_turn_angle": motion.get("one_time_turn_angle"),
            "leg_height_setting": motion.get("leg_height_setting"),
            "fall_state": motion.get("fall_state"),
            "left_wheel_speed": vehicle.get("left_speed_raw"),
            "right_wheel_speed": vehicle.get("right_speed_raw"),
            "head_yaw": head.get("yaw"),
            "head_pitch": head.get("pitch"),
            "joint_angles": joints,
            "is_on_car": business.get("is_on_car"),
            "is_on_charger": battery.get("on_charger_raw", business.get("is_on_charger")),
            "is_charging": battery.get("is_charging", business.get("is_charging")),
            "business_work_mode": business.get("work_mode"),
            "business_event": business.get("last_event"),
            "go_charge_result": go_charge_result,
            "ages": ages,
        }

    def snapshot(self, include_history=False):
        with self.lock:
            result = copy.deepcopy({key: value for key, value in self.data.items() if key != "graph_topics"})
            result["topics"] = {key: stats.snapshot(time.monotonic())
                                for key, stats in self.topics.items()}
            for key, sensor in result["sensors"].items():
                topic_key = self.config["sensors"][key]["topic_key"]
                topic = result["topics"][topic_key]
                if topic["status"] == "no_data" and sensor["publisher_count"] == 0 and sensor["topic_publisher_count"] == 0:
                    topic["status"] = "offline"
                sensor["topic"] = topic
                sensor["status"] = topic["status"]
                sensor["age_seconds"] = topic["age_seconds"]
                sensor["rate_hz"] = topic["rate_hz"]
            if include_history:
                result["history"] = {key: list(value) for key, value in self.history.items()}
            result["server_time"] = time.time()
            result["service_uptime_seconds"] = time.monotonic() - self.started_mono
            result["read_only"] = False
            return result

    def metadata(self):
        with self.lock:
            return {
                "name": "Zeroth M1 Dashboard",
                "version": "2.0.0",
                "read_only": False,
                "motion_control": True,
                "push_rate_hz": self.config["push_rate_hz"],
                "history_seconds": self.config["history_seconds"],
                "joints": copy.deepcopy(self.config["joints"]),
                "groups": copy.deepcopy(self.config["groups"]),
                "topics": copy.deepcopy(self.config["topics"]),
                "units": copy.deepcopy(self.config["units"]),
                "sources": copy.deepcopy(self.config["sources"]),
                "ros": {
                    "nodes": list(self.data["ros"]["nodes"]),
                    "topics": copy.deepcopy(self.data["graph_topics"]),
                },
            }
