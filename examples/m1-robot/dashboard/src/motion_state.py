"""Command lifecycle plus ROS observation for motion control.

Two facts are deliberately kept separate:
  * vendor HTTP success means "the vendor entry point accepted the command";
  * ROS feedback means "the robot was actually observed moving/stopping".

The command state machine only advances to ``completed`` after observation (or
immediately for commands with no observable physical effect).
"""

import copy
import threading
import time
from collections import deque


PHASES = [
    "created",
    "sending",
    "vendor_accepted",
    "observing",
    "observed_running",
    "observed_stopped",
    "completed",
]

FAILURE_PHASES = ["vendor_rejected", "connection_failed", "timed_out", "state_mismatch", "cancelled"]
FINAL_PHASES = set(PHASES[-1:]) | set(FAILURE_PHASES)

CONTROL_MODE_LABELS = {
    0: "待机",
    1: "步态行走",
    2: "预设动作",
    3: "速度控制",
}

GAIT_LABELS = {
    0: "停止",
    1: "前进",
    2: "后退",
    3: "原地左转",
    4: "原地右转",
    5: "向左平移",
    6: "向右平移",
    7: "中步幅前进",
    8: "上小车",
    9: "下小车",
    10: "跌倒爬起",
    11: "后仰爬起",
    12: "姿态复位（wbc 未实现）",
    13: "速度保持",
    14: "跨步",
    15: "单次转向",
    16: "调整腿长",
    17: "阻尼（预留）",
    53: "待机趴坐",
    62: "小车上分腿（调试）",
}

IDLE_GAITS = {0, 12, 16, 17, 53}
ARM_JOINT_INDICES = set(range(12, 18))

WHEEL_SPEED_THRESHOLD = 10.0
LINEAR_VELOCITY_THRESHOLD = 0.5
ANGULAR_RATE_THRESHOLD = 0.5
HEAD_ANGLE_THRESHOLD = 0.5
JOINT_ANGLE_THRESHOLD = 2.0
SETTLE_SECONDS = 0.6
STOP_MISMATCH_SECONDS = 2.5


class MotionState:
    def __init__(self, command_timeout_default=5.0, history_size=200, event_size=1000):
        self._lock = threading.RLock()
        self.command_timeout_default = command_timeout_default
        self._history_size = history_size
        self._events = deque(maxlen=event_size)
        self._archived = deque(maxlen=history_size)
        self._event_seq = 0
        self.command = None
        self.vendor = {
            "service": "unknown",
            "healthy": None,
            "battery": None,
            "last_probe_at": None,
            "last_success_at": None,
            "latency_ms": None,
            "error": None,
        }
        self.emergency = {
            "active": False,
            "updated_at": None,
            "request_id": None,
            "source": None,
        }
        self.last_observation = self._empty_observation()
        self._observation_at = None

    # -- vendor / robot state -------------------------------------------
    def note_vendor_probe(self, service, battery=None, latency_ms=None, error=None, healthy=None):
        with self._lock:
            self.vendor["service"] = service
            self.vendor["healthy"] = (service == "online") if healthy is None else healthy
            self.vendor["last_probe_at"] = time.time()
            self.vendor["latency_ms"] = latency_ms
            if battery is not None:
                self.vendor["battery"] = battery
            if service == "online":
                self.vendor["last_success_at"] = self.vendor["last_probe_at"]
                self.vendor["error"] = None
            else:
                self.vendor["error"] = error

    def set_emergency(self, active, request_id=None, source="dashboard"):
        with self._lock:
            changed = self.emergency["active"] != bool(active)
            self.emergency["active"] = bool(active)
            self.emergency["updated_at"] = time.time()
            self.emergency["request_id"] = request_id
            self.emergency["source"] = source
            if changed:
                self._emit("vendor", "emergency_stop" if active else "emergency_reset",
                           f"emergency {'engaged' if active else 'cleared'}",
                           request_id=request_id)

    def update_observation(self, observation, now=None):
        with self._lock:
            self.last_observation = observation or self._empty_observation()
            self._observation_at = now if now is not None else time.monotonic()

    # -- command lifecycle ----------------------------------------------
    def begin(self, command_def, request_id, client_request_id=None, reason=None, now_mono=None,
              hold_seconds=None):
        now = time.time()
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            if self.command and self.command.get("phase") not in FINAL_PHASES:
                raise RuntimeError("a motion command is already in progress")
            if self.command:
                self._archive_current_locked()

            expects_motion = self._expects_motion(command_def)
            timeout = float(command_def.get("timeout_seconds") or self.command_timeout_default)
            hold_duration = timeout
            if command_def.get("interaction") == "hold" and hold_seconds is not None:
                hold_duration = float(hold_seconds)
            observation_timeout = self._observation_timeout(
                command_def, max(timeout, hold_duration))
            command = {
                "request_id": request_id,
                "client_request_id": client_request_id,
                "command_id": command_def.get("id"),
                "vendor_command": command_def.get("vendor_command"),
                "display_name": command_def.get("display_name"),
                "category": command_def.get("category"),
                "interaction": command_def.get("interaction"),
                "phase": "created",
                "submitted_at": now,
                "vendor_responded_at": None,
                "motion_observed_at": None,
                "stopped_at": None,
                "completed_at": None,
                "last_update": now,
                "http_status": None,
                "latency_ms": None,
                "error": None,
                "detail": reason or "",
                "stop_requested": False,
                "stop_requested_at": None,
                "stop_accepted_at": None,
                "hold_deadline_at": None,
                "timeout_seconds": timeout,
                "hold_duration_seconds": hold_duration
                if command_def.get("interaction") == "hold" else None,
                "observation_timeout_seconds": observation_timeout,
                "expects_motion": expects_motion,
                "observation_result": "pending" if expects_motion else "not_required",
                "completion_signal": copy.deepcopy(command_def.get("completion_signal")),
                "motion_evidence": [],
                "locomotion_mode_at_start": self.locomotion_mode(self.last_observation),
                "locomotion_mode_observed": None,
                "archived": False,
                "_hold_deadline_mono": None,
                "_accepted_mono": None,
                "_started_observe_mono": None,
                "_last_motion_mono": None,
                "_stop_requested_mono": None,
                "_baseline": copy.deepcopy(self.last_observation),
                "_last_seen": None,
                "_effect": copy.deepcopy(command_def.get("physical_effect") or {}),
                "_completion": copy.deepcopy(command_def.get("completion_signal")),
                "_completion_runtime": {},
            }
            if command_def.get("interaction") == "hold":
                command["_hold_deadline_mono"] = now_mono + hold_duration
                command["hold_deadline_at"] = now + hold_duration
            self.command = command
            self._emit("dashboard", "created", f"{command['display_name']} created",
                       request_id=request_id, command_id=command["command_id"])
            return copy.deepcopy(self._public_command(command))

    def mark_sending(self):
        self._enter_phase("sending", "request sent to vendor service", source="dashboard")

    def mark_vendor_accepted(self, response, now_mono=None):
        mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            if not self.command:
                return
            self.command["vendor_responded_at"] = response.responded_at
            self.command["http_status"] = response.http_status
            self.command["latency_ms"] = response.latency_ms
            body = response.body if isinstance(response.body, dict) else {}
            self.command["_accepted_mono"] = mono
            if self.command["command_id"] == "emergency_stop":
                self._enter_phase("vendor_accepted", "emergency stop accepted")
                self._enter_phase("completed", "emergency stop accepted; no ROS motion expected")
                return
            if self.command["command_id"] == "motion_stop":
                self._enter_phase("observing", "MOTION_STOP accepted; checking locomotion feedback")
                return
            if not self.command["expects_motion"]:
                self._enter_phase("vendor_accepted", "command accepted", source="vendor",
                                  http_status=response.http_status, latency_ms=response.latency_ms,
                                  vendor_status=body.get("status"))
                self._enter_phase("completed", "no observable physical effect configured")
                return
            self._enter_phase("vendor_accepted",
                              f"HTTP {response.http_status} in {response.latency_ms:.1f}ms",
                              source="vendor", http_status=response.http_status,
                              latency_ms=response.latency_ms, vendor_status=body.get("status"))
            self._enter_phase("observing", "waiting for ROS feedback")
            self.command["_started_observe_mono"] = mono
            self.command["_last_motion_mono"] = mono

    def mark_stop_accepted(self, response, stop_request_id=None, now_mono=None):
        mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            if self.command and self.command.get("phase") not in FINAL_PHASES:
                self.command["stop_requested"] = True
                self.command["stop_requested_at"] = response.responded_at
                self.command["stop_accepted_at"] = response.responded_at
                self.command["_stop_requested_mono"] = mono
                self.command["stop_request_id"] = stop_request_id
                self._emit("vendor", "stop_accepted",
                           f"MOTION_STOP HTTP {response.http_status} in {response.latency_ms:.1f}ms",
                           request_id=self.command["request_id"], http_status=response.http_status)
                if self.command.get("interaction") == "workflow":
                    if (self.command.get("phase") in ("observing", "observed_stopped")
                            and not self.command.get("motion_observed_at")):
                        self.command["observation_result"] = "cancelled"
                        self._enter_phase(
                            "cancelled",
                            "vendor accepted stop; workflow cancelled before movement was confirmed",
                        )
                    return
                if self.command.get("phase") == "observing" and not self.command.get("motion_observed_at"):
                    self.command["observation_result"] = "not_confirmed"
                    self._enter_phase("observed_stopped", "stop accepted before movement telemetry was confirmed")
                    self._enter_phase("completed", "vendor accepted stop; prior movement was not confirmed by available telemetry")

    def mark_vendor_error(self, error, phase=None):
        with self._lock:
            if not self.command:
                return
            code = getattr(error, "code", "vendor_error")
            if phase is None:
                phase = {
                    "connection_failed": "connection_failed",
                    "timed_out": "timed_out",
                    "vendor_http_error": "vendor_rejected",
                    "vendor_protocol_error": "vendor_rejected",
                }.get(code, "vendor_rejected")
            self.command["vendor_responded_at"] = getattr(error, "responded_at", time.time())
            self.command["http_status"] = getattr(error, "http_status", None)
            self.command["latency_ms"] = getattr(error, "latency_ms", None)
            self.command["error"] = error.as_dict() if hasattr(error, "as_dict") else {"code": code, "message": str(error)}
            self._enter_phase(phase, getattr(error, "message", str(error)))

    def note_stop_requested(self, request_id=None, now_mono=None):
        mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            if self.command and self.command.get("phase") not in FINAL_PHASES:
                self.command["stop_requested"] = True
                self.command["stop_requested_at"] = time.time()
                self.command["_stop_requested_mono"] = mono
                self.command["stop_request_id"] = request_id
                self._emit("dashboard", "stop_requested", "operator released hold / pressed stop",
                           request_id=self.command["request_id"])

    # -- periodic evaluation --------------------------------------------
    def tick(self, observation, now_mono=None):
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            self.update_observation(observation, now_mono)
            command = self.command
            if not command or command.get("phase") in FINAL_PHASES:
                return
            phase = command["phase"]
            baseline = command.get("_baseline") or {}
            previous = command.get("_last_seen")
            expects_motion = command.get("expects_motion")
            effect = self._effect_of(command)
            motion, reasons = self._detect_motion(
                observation, baseline, previous, effect, expects_motion, phase == "observing")
            command["_last_seen"] = copy.deepcopy(observation)

            if command.get("command_id") == "motion_stop":
                if not self._locomotion_active(observation):
                    self._enter_phase("observed_stopped", "locomotion feedback is stationary")
                    self._enter_phase("completed", "stop confirmed by ROS feedback")
                elif command.get("_accepted_mono") and now_mono - command["_accepted_mono"] > STOP_MISMATCH_SECONDS:
                    command["error"] = {
                        "code": "state_mismatch",
                        "message": "MOTION_STOP accepted but wheel feedback still reports motion",
                    }
                    self._enter_phase("state_mismatch", command["error"]["message"], source="ros")
                return

            fall = observation.get("fall_state")
            baseline_fall = baseline.get("fall_state")
            if fall not in (None, 0) and baseline_fall in (None, 0) and command.get("command_id") != "get_up":
                command["error"] = {
                    "code": "state_mismatch",
                    "message": f"fall state changed to {fall} while command was active",
                }
                self._enter_phase("state_mismatch", command["error"]["message"], source="ros")
                return

            if (command.get("interaction") == "workflow"
                    and command.get("stop_requested")
                    and command.get("stop_accepted_at") is not None):
                if not self._locomotion_active(observation):
                    command["observation_result"] = "cancelled"
                    self._enter_phase(
                        "cancelled",
                        "vendor accepted stop and locomotion feedback is stationary",
                        source="ros",
                    )
                    return
                stopped_for = now_mono - (command.get("_stop_requested_mono") or now_mono)
                if stopped_for > STOP_MISMATCH_SECONDS:
                    command["error"] = {
                        "code": "state_mismatch",
                        "message": "stop accepted but wheel feedback still reports motion",
                    }
                    self._enter_phase("state_mismatch", command["error"]["message"], source="ros")
                return

            completion = command.get("_completion")
            if completion and command.get("_accepted_mono") is not None:
                outcome, detail = self._completion_outcome(command, observation, completion)
                if outcome == "failed":
                    command["error"] = {"code": "state_mismatch", "message": detail}
                    self._enter_phase("state_mismatch", detail, source="business")
                    return
                if outcome == "completed":
                    command["observation_result"] = "business_and_hardware_confirmed"
                    self._enter_phase("completed", detail, source="business")
                    return
                failure, failure_detail = self._completion_failure_outcome(
                    command, observation, completion, now_mono)
                if failure:
                    command["error"] = {"code": "state_mismatch", "message": failure_detail}
                    self._enter_phase("state_mismatch", failure_detail, source="business")
                    return
                elapsed = now_mono - command["_accepted_mono"]
                if elapsed > command.get("timeout_seconds", 30):
                    detail = f"business completion signal not received within {elapsed:.1f}s"
                    command["error"] = {"code": "timed_out", "message": detail}
                    self._enter_phase("timed_out", detail, source="business")
                    return

            if phase == "observing":
                if motion:
                    command["motion_observed_at"] = time.time()
                    command["observation_result"] = "observed_moving"
                    command["motion_evidence"] = list(reasons)
                    command["locomotion_mode_observed"] = self.locomotion_mode(observation)
                    command["_last_motion_mono"] = now_mono
                    self._enter_phase("observed_running", "; ".join(reasons), source="ros")
                elif command.get("stop_requested"):
                    if not self._locomotion_active(observation):
                        command["observation_result"] = "not_confirmed"
                        self._enter_phase("observed_stopped", "stop confirmed before movement telemetry was available")
                        self._enter_phase("completed", "vendor accepted; physical movement was not confirmed by available telemetry")
                elif not completion and command.get("_started_observe_mono") is not None:
                    elapsed = now_mono - command["_started_observe_mono"]
                    if elapsed > command.get("observation_timeout_seconds", 10):
                        self._enter_phase(
                            "timed_out",
                            f"vendor accepted but no ROS observation within {elapsed:.1f}s",
                            source="ros",
                        )
                        command["error"] = {
                            "code": "timed_out",
                            "message": "vendor accepted the command but ROS feedback showed no motion",
                        }
                        command["observation_result"] = "not_confirmed"
                return

            if phase == "observed_running":
                if motion:
                    command["_last_motion_mono"] = now_mono
                else:
                    last = command.get("_last_motion_mono") or now_mono
                    if now_mono - last >= SETTLE_SECONDS:
                        self._enter_phase("observed_stopped", "motion settled", source="ros")
                        command["stopped_at"] = time.time()
                if command.get("stop_requested") and motion:
                    if now_mono - (command.get("_stop_requested_mono") or now_mono) > STOP_MISMATCH_SECONDS:
                        command["error"] = {
                            "code": "state_mismatch",
                            "message": "stop requested but wheel feedback still reports motion",
                        }
                        self._enter_phase("state_mismatch", command["error"]["message"], source="ros")
                if command.get("_accepted_mono") and now_mono - command["_accepted_mono"] > command.get("timeout_seconds", 30):
                    self._enter_phase("timed_out", "command exceeded its configured timeout", source="ros")
                    command["error"] = {"code": "timed_out", "message": "command did not settle before timeout"}
                return

            if phase == "observed_stopped":
                if completion:
                    return
                self._enter_phase("completed", "motion lifecycle complete", source="ros")
                command["completed_at"] = time.time()

    def hold_expired(self, now_mono=None):
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            command = self.command
            if not command or command.get("phase") in FINAL_PHASES:
                return None
            deadline = command.get("_hold_deadline_mono")
            if deadline is None or command.get("stop_requested"):
                return None
            if now_mono >= deadline:
                return command["request_id"]
            return None

    def is_busy(self):
        with self._lock:
            return bool(self.command and self.command.get("phase") not in FINAL_PHASES)

    def snapshot(self):
        with self._lock:
            command = copy.deepcopy(self.command) if self.command else None
            if command:
                command = self._public_command(command)
            observation = copy.deepcopy(self.last_observation)
            ages = observation.get("ages") or {}
            robot = {
                "control_mode": observation.get("control_mode"),
                "control_mode_label": self._label(CONTROL_MODE_LABELS, observation.get("control_mode")),
                "locomotion_mode": self.locomotion_mode(observation),
                "wheel_speed_applicable": self.wheel_speed_applicable(observation),
                "gait_type": observation.get("gait_type"),
                "gait_type_label": self._label(GAIT_LABELS, observation.get("gait_type")),
                "fall_state": observation.get("fall_state"),
                "left_wheel_speed": observation.get("left_wheel_speed"),
                "right_wheel_speed": observation.get("right_wheel_speed"),
                "linear_velocity_x": observation.get("linear_velocity_x"),
                "angular_rate_z": observation.get("angular_rate_z"),
                "head_yaw": observation.get("head_yaw"),
                "head_pitch": observation.get("head_pitch"),
                "is_on_car": observation.get("is_on_car"),
                "is_on_charger": observation.get("is_on_charger"),
                "is_charging": observation.get("is_charging"),
                "business_work_mode": observation.get("business_work_mode"),
                "business_event": copy.deepcopy(observation.get("business_event")),
                "go_charge_result": copy.deepcopy(observation.get("go_charge_result")),
                "data_age_seconds": {
                    "motion_feedback": ages.get("motion_feedback"),
                    "self_fall": ages.get("self_fall"),
                    "vehicle": ages.get("vehicle"),
                    "head": ages.get("head"),
                    "kinematic": ages.get("kinematic"),
                },
            }
            return {
                "vendor_service": self.vendor.get("service", "unknown"),
                "vendor": copy.deepcopy(self.vendor),
                "emergency": copy.deepcopy(self.emergency),
                "command": command,
                "robot": robot,
            }

    def events(self, limit=100):
        limit = max(1, min(int(limit), 500))
        with self._lock:
            return list(reversed(list(self._events)))[:limit]

    def commands(self, limit=50):
        limit = max(1, min(int(limit), 200))
        with self._lock:
            items = list(reversed(list(self._archived)))[:limit]
            if self.command and not self.command.get("archived"):
                items = [self._public_command(self.command)] + items
                items = items[:limit]
            return items

    # -- internals -------------------------------------------------------
    def _enter_phase(self, phase, detail, source="dashboard", **extra):
        command = self.command
        if not command:
            return
        if command.get("phase") == phase:
            command["detail"] = detail
            command["last_update"] = time.time()
            return
        command["phase"] = phase
        command["detail"] = detail
        command["last_update"] = time.time()
        if phase == "observed_stopped" and not command.get("stopped_at"):
            command["stopped_at"] = command["last_update"]
        if phase == "completed" and not command.get("completed_at"):
            command["completed_at"] = command["last_update"]
        if phase in FINAL_PHASES and not command.get("archived"):
            command["archived"] = True
            self._archived.append(copy.deepcopy(self._public_command(command)))
        self._emit(source, phase, detail, request_id=command.get("request_id"),
                   command_id=command.get("command_id"), **extra)

    def _archive_current_locked(self):
        if self.command and not self.command.get("archived"):
            self.command["archived"] = True
            self._archived.append(copy.deepcopy(self._public_command(self.command)))

    def _emit(self, source, phase, detail, **extra):
        self._event_seq += 1
        event = {
            "seq": self._event_seq,
            "at": time.time(),
            "source": source,
            "phase": phase,
            "detail": detail,
        }
        event.update({key: value for key, value in extra.items() if value is not None})
        self._events.append(event)

    @staticmethod
    def _public_command(command):
        return {key: copy.deepcopy(value) for key, value in command.items() if not key.startswith("_")}

    @staticmethod
    def _label(table, value):
        if value is None:
            return None
        return table.get(int(value), f"未知({value})")

    @staticmethod
    def _expects_motion(command_def):
        effect = command_def.get("physical_effect") or {}
        return any(bool(effect.get(part)) for part in ("head", "arms", "legs", "base"))

    @staticmethod
    def _observation_timeout(command_def, timeout):
        interaction = command_def.get("interaction")
        category = command_def.get("category")
        if category == "direction":
            # Give the hold watchdog a chance to auto-stop first.
            return timeout + 2.0
        if interaction == "workflow":
            return min(timeout, 30.0)
        return min(timeout, 10.0)

    @staticmethod
    def _effect_of(command):
        effect = command.get("_effect")
        if isinstance(effect, dict) and effect:
            return effect
        return {"head": False, "arms": False, "legs": False, "base": False}

    @staticmethod
    def _completion_outcome(command, observation, completion):
        event_rule = completion.get("event") or {}
        event = observation.get("business_event") or {}
        event_is_current = (
            isinstance(event, dict)
            and isinstance(event.get("received_at"), (int, float))
            and event["received_at"] >= command.get("submitted_at", 0)
        )
        event_matches = event_is_current and all(
            event.get(key) == expected
            for key, expected in event_rule.items()
            if key != "status"
        )
        if event_matches and event.get("status") in ("failed", "failure", "error"):
            reason = event.get("reason") or event.get("error_code") or "vendor workflow failed"
            return "failed", f"vendor business event reported failure: {reason}"

        success_status = event_rule.get("status", "success")
        if not event_matches or event.get("status") != success_status:
            return "pending", "waiting for vendor business success event"

        mismatches = []
        for key, expected in (completion.get("state") or {}).items():
            actual = observation.get(key)
            if actual != expected:
                mismatches.append(f"{key}={actual} (expected {expected})")
        if mismatches:
            return "pending", "business event succeeded; waiting for " + ", ".join(mismatches)
        return "completed", "vendor business event and hardware state confirmed completion"

    @staticmethod
    def _completion_failure_outcome(command, observation, completion, now_mono):
        """Detect authoritative workflow failure without weakening success checks."""
        failure = completion.get("failure") or {}
        result_rule = failure.get("result") or {}
        result = observation.get(result_rule.get("field")) or {}
        result_is_current = (
            isinstance(result, dict)
            and isinstance(result.get("received_at"), (int, float))
            and result["received_at"] >= command.get("submitted_at", 0)
        )
        if result_is_current and result.get("value") in result_rule.get("values", []):
            return True, (
                f"vendor workflow result reported failure: "
                f"{result_rule.get('field')}={result.get('value')}"
            )

        mode_rule = failure.get("work_mode") or {}
        if not mode_rule:
            return False, None
        runtime = command.setdefault("_completion_runtime", {})
        mode = observation.get("business_work_mode")
        if mode == mode_rule.get("active"):
            runtime["active_work_mode_seen"] = True
            runtime["idle_since_mono"] = None
            return False, None
        if runtime.get("active_work_mode_seen") and mode == mode_rule.get("idle"):
            if runtime.get("idle_since_mono") is None:
                runtime["idle_since_mono"] = now_mono
                return False, None
            grace = float(mode_rule.get("grace_seconds", 3.0))
            if now_mono - runtime["idle_since_mono"] >= grace:
                return True, (
                    f"vendor workflow returned to {mode!r} without the configured success signal "
                    f"or hardware state after {grace:.1f}s"
                )
        return False, None

    def _detect_motion(self, observation, baseline, previous, effect, expects_motion, compare_to_baseline):
        if not expects_motion:
            return False, []
        reasons = []
        # While waiting for movement, compare against the command-start baseline so
        # slow walking accumulates enough joint change to be observed. Once running,
        # compare adjacent samples so a settled pose can still be detected.
        reference = baseline if compare_to_baseline or not previous else previous
        if effect.get("base") or effect.get("legs"):
            if self._base_moving(observation):
                reasons.append("wheel telemetry indicates motion")
            control_mode = observation.get("control_mode")
            gait_type = observation.get("gait_type")
            if control_mode != reference.get("control_mode"):
                reasons.append(f"control_mode {reference.get('control_mode')}→{control_mode}")
            if gait_type != reference.get("gait_type"):
                reasons.append(f"gait_type {reference.get('gait_type')}→{gait_type}")
                if gait_type is not None and gait_type not in IDLE_GAITS:
                    reasons.append("walking gait active")
            elif (compare_to_baseline and control_mode == 1
                  and gait_type is not None and gait_type not in IDLE_GAITS):
                reasons.append("walking gait active")
            linear_velocity = observation.get("linear_velocity_x")
            if isinstance(linear_velocity, (int, float)) and abs(float(linear_velocity)) >= LINEAR_VELOCITY_THRESHOLD:
                reasons.append(f"linear velocity {linear_velocity}")
            angular_rate = observation.get("angular_rate_z")
            if isinstance(angular_rate, (int, float)) and abs(float(angular_rate)) >= ANGULAR_RATE_THRESHOLD:
                reasons.append(f"angular rate {angular_rate}")
        if effect.get("head"):
            yaw_delta = self._delta(observation.get("head_yaw"), reference.get("head_yaw"))
            pitch_delta = self._delta(observation.get("head_pitch"), reference.get("head_pitch"))
            if max(yaw_delta, pitch_delta) >= HEAD_ANGLE_THRESHOLD:
                reasons.append(f"head moved Δ{max(yaw_delta, pitch_delta):.1f}°")
        if effect.get("arms"):
            delta = self._joint_delta(observation.get("joint_angles"), reference.get("joint_angles"),
                                      ARM_JOINT_INDICES)
            if delta >= JOINT_ANGLE_THRESHOLD:
                reasons.append(f"arm joint moved Δ{delta:.1f}°")
        if effect.get("legs"):
            delta = self._joint_delta(observation.get("joint_angles"), reference.get("joint_angles"), None)
            if delta >= JOINT_ANGLE_THRESHOLD:
                reasons.append(f"joint moved Δ{delta:.1f}°")
        return bool(reasons), reasons

    @staticmethod
    def _base_moving(observation):
        left = observation.get("left_wheel_speed")
        right = observation.get("right_wheel_speed")
        speeds = [abs(float(value)) for value in (left, right) if isinstance(value, (int, float))]
        return bool(speeds) and max(speeds) >= WHEEL_SPEED_THRESHOLD

    @classmethod
    def _locomotion_active(cls, observation):
        if cls._base_moving(observation):
            return True
        gait_type = observation.get("gait_type")
        if observation.get("control_mode") == 1 and gait_type is not None and gait_type not in IDLE_GAITS:
            return True
        linear_velocity = observation.get("linear_velocity_x")
        angular_rate = observation.get("angular_rate_z")
        return (
            isinstance(linear_velocity, (int, float))
            and abs(float(linear_velocity)) >= LINEAR_VELOCITY_THRESHOLD
        ) or (
            isinstance(angular_rate, (int, float))
            and abs(float(angular_rate)) >= ANGULAR_RATE_THRESHOLD
        )

    @classmethod
    def locomotion_mode(cls, observation):
        if cls._base_moving(observation):
            return "wheeled"
        if observation.get("control_mode") == 1:
            return "walking"
        if observation.get("control_mode") == 3:
            return "velocity_control"
        if observation.get("control_mode") == 0:
            return "idle"
        return "unknown"

    @classmethod
    def wheel_speed_applicable(cls, observation):
        mode = cls.locomotion_mode(observation)
        if mode == "walking":
            return False
        if mode == "wheeled":
            return True
        return None

    @staticmethod
    def _delta(current, baseline):
        if isinstance(current, (int, float)) and isinstance(baseline, (int, float)):
            return abs(float(current) - float(baseline))
        return 0.0

    @staticmethod
    def _joint_delta(current, baseline, indices):
        if not isinstance(current, list) or not isinstance(baseline, list):
            return 0.0
        deltas = []
        for index, value in enumerate(current):
            if index >= len(baseline) or not isinstance(value, (int, float)):
                continue
            if indices is not None and index not in indices:
                continue
            deltas.append(abs(float(value) - float(baseline[index])))
        return max(deltas) if deltas else 0.0

    @staticmethod
    def _empty_observation():
        return {
            "control_mode": None,
            "gait_type": None,
            "linear_velocity_x": None,
            "angular_rate_z": None,
            "fall_state": None,
            "left_wheel_speed": None,
            "right_wheel_speed": None,
            "head_yaw": None,
            "head_pitch": None,
            "joint_angles": [],
            "is_on_car": None,
            "is_on_charger": None,
            "is_charging": None,
            "business_work_mode": None,
            "business_event": None,
            "go_charge_result": None,
            "ages": {},
        }
