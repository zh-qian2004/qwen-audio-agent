"""Orchestrates vendor commands, the ROS-observation state machine and audit.

Public surface used by FastAPI:
    capabilities(), status(), history(), command(), stop(), emergency_stop(),
    reset_emergency(), start(), shutdown()
"""

import logging
import threading
import time
import uuid

from motion_state import FAILURE_PHASES, PHASES, MotionState
from vendor_control import (
    VendorConnectionError,
    VendorControlError,
    VendorHttpError,
    VendorProtocolError,
    VendorResponse,
)


LOG = logging.getLogger("dashboard.motion")

HOLD_COMMANDS = {"move_forward", "move_backward", "strafe_left", "strafe_right",
                 "rotate_left", "rotate_right"}
EMERGENCY_ALLOWED = {"motion_stop", "emergency_stop", "get_up"}
IDEMPOTENCY_TTL = 15.0
IDEMPOTENCY_MAX = 128


class MotionControlError(Exception):
    def __init__(self, message, status_code=400, code="invalid_request"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    def as_dict(self):
        return {"code": self.code, "message": self.message}


class MotionController:
    def __init__(self, config, store, audit, vendor_client=None, base_url=None,
                 business_publisher=None):
        self.config = config
        self.store = store
        self.audit = audit
        self.meta = config.get("meta", {})
        self.commands = {}
        for item in config.get("commands", []):
            self.commands[item["id"]] = item
        if vendor_client is None:
            from vendor_control import VendorControlClient
            vendor_client = VendorControlClient()
        self.vendor = vendor_client
        self.business_publisher = business_publisher
        self.motion = MotionState(
            command_timeout_default=float(self.meta.get("command_timeout_seconds", 5)),
            history_size=int(self.meta.get("history_limit", 200)),
        )
        self.base_url = base_url or self.meta.get("vendor_base_url", "http://127.0.0.1:12321")
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._threads = []
        self._idempotency = {}
        self._active_hold = None
        self._last_vendor_state = None
        self._vendor_status_body = None
        self._vendor_status_at = None

    # -- lifecycle -------------------------------------------------------
    def start(self):
        if self._threads:
            return
        for target, name in ((self._health_loop, "motion-health"),
                             (self._monitor_loop, "motion-monitor")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def shutdown(self):
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []

    # -- read-only API ---------------------------------------------------
    def capabilities(self):
        categories = []
        commands_by_category = {}
        for item in self.config.get("commands", []):
            commands_by_category.setdefault(item["category"], []).append(item)
        for category in self.meta.get("categories", []):
            entry = dict(category)
            entry["commands"] = commands_by_category.get(category["id"], [])
            categories.append(entry)
        return {
            "base_url": self.base_url,
            "categories": categories,
            "phases": PHASES + FAILURE_PHASES,
            "interactions": self.meta.get("interactions", {}),
            "verification_states": self.meta.get("verification_states", {}),
            "control_mode_labels": self.meta.get("control_mode_labels", {}),
            "gait_labels": self.meta.get("gait_labels", {}),
            "limits": {
                "max_hold_seconds": self.meta.get("max_hold_seconds", 10),
                "history_limit": self.meta.get("history_limit", 200),
            },
        }

    def status(self):
        snapshot = self.motion.snapshot()
        snapshot["phases"] = PHASES + FAILURE_PHASES
        snapshot["vendor_api"] = {
            "base_url": self.base_url,
            "control_path": "/api/control",
            "emergency_stop_path": "/api/emergency_stop",
            "emergency_reset_path": "/api/reset_emergency",
            "last_status": self._vendor_status_body,
            "last_status_at": self._vendor_status_at,
        }
        snapshot["updated_at"] = time.time()
        return snapshot

    def history(self, limit=100):
        limit = max(1, min(int(limit), 200))
        return {
            "commands": self.motion.commands(limit),
            "events": self.motion.events(limit),
            "audit": self.audit.tail(min(limit, 100)),
            "phases": PHASES + FAILURE_PHASES,
        }

    # -- command dispatch ------------------------------------------------
    def command(self, command_id, client_request_id=None, reason=None, actor="unknown",
                duration_seconds=None):
        command_def = self._lookup(command_id)
        duration_seconds = self._validate_duration(command_def, duration_seconds)
        reason = (reason or "dashboard manual control")[:500]
        if client_request_id:
            client_request_id = str(client_request_id)[:64]
        with self._lock:
            replay = self._idempotent_lookup(client_request_id, command_id)
            if replay is not None:
                replay = dict(replay)
                replay["idempotent_replay"] = True
                return replay
            self._check_ready(command_def)
            request_id = str(uuid.uuid4())
            self.motion.begin(
                command_def, request_id, client_request_id, reason,
                hold_seconds=duration_seconds,
            )
            if command_def.get("interaction") == "hold":
                self._active_hold = {"request_id": request_id, "command_id": command_id}

        self.motion.mark_sending()
        self._audit("command_request", {
            "request_id": request_id,
            "client_request_id": client_request_id,
            "command_id": command_id,
            "vendor_command": command_def["vendor_command"],
            "interaction": command_def.get("interaction"),
            "duration_ms": round(duration_seconds * 1000) if duration_seconds is not None else None,
            "reason": reason,
            "actor": actor,
        })

        try:
            if command_def.get("transport") == "business_app_topic":
                response = self._publish_business_request(command_def, request_id)
            else:
                response = self.vendor.send_command(command_def["vendor_command"], request_id)
        except VendorControlError as error:
            self.motion.mark_vendor_error(error)
            if command_def.get("interaction") == "hold":
                with self._lock:
                    self._active_hold = None
            result = self._error_response(command_id, command_def, request_id,
                                          client_request_id, error)
            self._audit("vendor_error", {
                "request_id": request_id,
                "command_id": command_id,
                "vendor_command": command_def["vendor_command"],
                "error": error.as_dict(),
                "reason": reason,
                "actor": actor,
            })
            self._idempotent_store(client_request_id, command_id, result)
            return result

        self.motion.mark_vendor_accepted(response)
        self._audit("vendor_response", {
            "request_id": request_id,
            "command_id": command_id,
            "vendor_command": command_def["vendor_command"],
            "http_status": response.http_status,
            "latency_ms": response.latency_ms,
            "vendor_body": response.body,
            "reason": reason,
            "actor": actor,
        })
        result = self._success_response(
            command_id, command_def, request_id, client_request_id, response,
            extra={"duration_ms": round(duration_seconds * 1000)}
            if duration_seconds is not None else None,
        )
        self._idempotent_store(client_request_id, command_id, result)
        return result

    def stop(self, reason=None, actor="unknown", client_request_id=None):
        reason = (reason or "manual MOTION_STOP")[:500]
        request_id = str(uuid.uuid4())
        with self._lock:
            self._active_hold = None
            replay = self._idempotent_lookup(client_request_id, "motion_stop")
            if replay is not None:
                replay = dict(replay)
                replay["idempotent_replay"] = True
                return replay
        self.motion.note_stop_requested(request_id)
        self._audit("stop_request", {
            "request_id": request_id,
            "reason": reason,
            "actor": actor,
            "priority": "high",
        })
        try:
            response = self.vendor.send_command("MOTION_STOP", request_id)
        except VendorControlError as error:
            self.motion.mark_vendor_error(error)
            result = self._stop_error_response(request_id, error)
            self._audit("stop_error", {"request_id": request_id, "error": error.as_dict(),
                                      "reason": reason, "actor": actor})
            self._idempotent_store(client_request_id, "motion_stop", result)
            return result

        self.motion.mark_stop_accepted(response, request_id)
        self._audit("stop_response", {
            "request_id": request_id,
            "http_status": response.http_status,
            "latency_ms": response.latency_ms,
            "vendor_body": response.body,
            "reason": reason,
            "actor": actor,
        })
        result = self._stop_success_response(request_id, response)
        self._idempotent_store(client_request_id, "motion_stop", result)
        return result

    def emergency_stop(self, reason=None, actor="unknown"):
        reason = (reason or "EMERGENCY_STOP")[:500]
        request_id = str(uuid.uuid4())
        command_def = self.commands.get("emergency_stop", {
            "id": "emergency_stop",
            "vendor_command": "EMERGENCY_STOP",
            "display_name": "紧急停止",
        })
        self._audit("emergency_stop_request", {
            "request_id": request_id, "reason": reason, "actor": actor, "priority": "highest",
        })
        try:
            try:
                response = self.vendor.emergency_stop(request_id)
                route = "/api/emergency_stop"
            except VendorHttpError as error:
                if error.http_status not in (404, 405):
                    raise
                LOG.warning("dedicated emergency route unavailable (%s); falling back to /api/control",
                            error.http_status)
                response = self.vendor.send_command("EMERGENCY_STOP", request_id)
                route = "/api/control (fallback)"
        except VendorControlError as error:
            self._audit("emergency_stop_error", {
                "request_id": request_id, "error": error.as_dict(),
                "reason": reason, "actor": actor,
            })
            return self._error_response("emergency_stop", command_def, request_id, None, error)

        self.motion.set_emergency(True, request_id, source="dashboard")
        self._audit("emergency_stop_response", {
            "request_id": request_id,
            "route": route,
            "http_status": response.http_status,
            "latency_ms": response.latency_ms,
            "vendor_body": response.body,
            "reason": reason,
            "actor": actor,
        })
        return self._success_response("emergency_stop", command_def, request_id, None, response,
                                      extra={
                                          "route": route,
                                          "emergency_active": True,
                                          "execution_state": "accepted",
                                          "completion_confirmed": False,
                                      })

    def reset_emergency(self, reason=None, actor="unknown"):
        reason = (reason or "reset emergency")[:500]
        request_id = str(uuid.uuid4())
        command_def = self.commands.get("reset", {
            "id": "emergency_reset",
            "vendor_command": "RESET",
            "display_name": "恢复",
        })
        self._audit("emergency_reset_request", {
            "request_id": request_id, "reason": reason, "actor": actor,
        })
        try:
            try:
                response = self.vendor.reset_emergency(request_id)
                route = "/api/reset_emergency"
            except VendorHttpError as error:
                if error.http_status not in (404, 405):
                    raise
                LOG.warning("dedicated reset route unavailable (%s); falling back to /api/control RESET",
                            error.http_status)
                response = self.vendor.send_command("RESET", request_id)
                route = "/api/control (fallback)"
        except VendorControlError as error:
            self._audit("emergency_reset_error", {
                "request_id": request_id, "error": error.as_dict(),
                "reason": reason, "actor": actor,
            })
            return self._error_response("emergency_reset", command_def, request_id, None, error)

        self.motion.set_emergency(False, request_id, source="dashboard")
        self._audit("emergency_reset_response", {
            "request_id": request_id,
            "route": route,
            "http_status": response.http_status,
            "latency_ms": response.latency_ms,
            "vendor_body": response.body,
            "reason": reason,
            "actor": actor,
        })
        return self._success_response("emergency_reset", command_def, request_id, None, response,
                                      extra={"route": route, "emergency_active": False})

    # -- validation ------------------------------------------------------
    def _lookup(self, command_id):
        if not command_id or command_id not in self.commands:
            raise MotionControlError(f"unknown command_id: {command_id}", 400, "unknown_command")
        command_def = self.commands[command_id]
        if not command_def.get("dashboard_enabled", True):
            raise MotionControlError(
                f"{command_id} is registered but not enabled in the dashboard "
                f"(verification_state={command_def.get('verification_state')})",
                403, "command_disabled",
            )
        return command_def

    def _check_ready(self, command_def):
        if self.motion.is_busy():
            raise MotionControlError("another motion command is still in progress", 409, "busy")
        if self.motion.emergency.get("active") and command_def["id"] not in EMERGENCY_ALLOWED:
            raise MotionControlError("emergency stop is active; reset before sending other commands",
                                     409, "emergency_active")
        observation = self._observation()
        for key, expected in (command_def.get("required_state") or {}).items():
            actual = observation.get(key)
            if actual is None:
                raise MotionControlError(
                    f"cannot verify required robot state {key}={expected}",
                    409, "state_unknown",
                )
            if actual != expected:
                raise MotionControlError(
                    f"command requires {key}={expected}, current value is {actual}",
                    409, "precondition_failed",
                )
        for key, forbidden in (command_def.get("forbidden_state") or {}).items():
            actual = observation.get(key)
            if actual is None:
                raise MotionControlError(
                    f"cannot verify forbidden robot state {key}={forbidden}",
                    409, "state_unknown",
                )
            if actual == forbidden:
                raise MotionControlError(
                    command_def.get("forbidden_state_message")
                    or f"command is unavailable when {key}={forbidden}",
                    409, command_def.get("forbidden_state_code") or "precondition_failed",
                )
        fall_state = observation.get("fall_state")
        effect = command_def.get("physical_effect") or {}
        if fall_state not in (None, 0) and (effect.get("legs") or effect.get("base")) \
                and command_def["id"] != "get_up":
            raise MotionControlError(
                f"robot reports fall_state={fall_state}; only GET_UP/STOP are allowed",
                409, "fall_detected",
            )

    def _publish_business_request(self, command_def, request_id):
        payload = command_def.get("business_request")
        if not isinstance(payload, dict):
            raise VendorConnectionError(
                "business request payload is not configured",
                request_id=request_id, submitted_at=time.time(), responded_at=time.time(),
            )
        if self.business_publisher is None:
            raise VendorConnectionError(
                "ROS business publisher is unavailable",
                request_id=request_id, submitted_at=time.time(), responded_at=time.time(),
            )
        submitted_at = time.time()
        started = time.monotonic()
        try:
            raw = self.business_publisher(payload)
        except Exception as error:
            responded_at = time.time()
            raise VendorConnectionError(
                f"failed to publish business request: {error}",
                request_id=request_id, submitted_at=submitted_at,
                responded_at=responded_at,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail=str(error),
            ) from error
        responded_at = time.time()
        return VendorResponse(
            request_id=request_id,
            method="PUBLISH",
            path="/BusinessAppReq_topic",
            submitted_at=submitted_at,
            responded_at=responded_at,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            http_status=202,
            content_type="application/json",
            body={"status": "accepted", "transport": "ros2_topic", "payload": payload},
            raw_body=raw,
        )

    def _validate_duration(self, command_def, duration_seconds):
        if duration_seconds is None:
            return None
        if command_def.get("interaction") != "hold":
            raise MotionControlError(
                "duration_ms is only supported for hold movement commands",
                400, "duration_not_supported",
            )
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError):
            raise MotionControlError("duration_ms must be numeric", 400, "invalid_duration")
        maximum = float(self.meta.get("max_hold_seconds", 10))
        if duration < 0.2 or duration > maximum:
            raise MotionControlError(
                f"duration_ms must be between 200 and {round(maximum * 1000)}",
                400, "invalid_duration",
            )
        return duration

    # -- health monitor --------------------------------------------------
    def _health_loop(self):
        counter = 0
        while not self._stop_event.is_set():
            service = "offline"
            battery = None
            latency = None
            error_text = None
            try:
                response = self.vendor.battery()
                battery = response.body
                latency = response.latency_ms
                service = "online"
            except VendorControlError as error:
                error_text = error.as_dict()
                try:
                    health = self.vendor.health()
                    if health.http_status == 200:
                        service = "degraded"
                        latency = health.latency_ms
                except VendorControlError:
                    service = "offline"
            except Exception as error:  # pragma: no cover - defensive
                error_text = {"code": "unexpected", "message": str(error)}
                LOG.exception("vendor health probe failed")
            self.motion.note_vendor_probe(service, battery=battery, latency_ms=latency,
                                          error=error_text)
            if service != self._last_vendor_state:
                LOG.info("vendor service state: %s", service)
                self._last_vendor_state = service
            counter += 1
            if counter % 5 == 0:
                try:
                    status = self.vendor.status()
                    self._vendor_status_body = status.body
                    self._vendor_status_at = status.responded_at
                except VendorControlError:
                    self._vendor_status_body = None
            self._stop_event.wait(2.0)

    # -- motion monitor --------------------------------------------------
    def _monitor_loop(self):
        while not self._stop_event.is_set():
            try:
                observation = self._observation()
                self.motion.tick(observation)
                expired = self.motion.hold_expired()
                if expired:
                    self._auto_stop(expired)
                if not self.motion.is_busy():
                    with self._lock:
                        self._active_hold = None
            except Exception:  # pragma: no cover - keep monitor alive
                LOG.exception("motion monitor tick failed")
            self._stop_event.wait(0.1)

    def _auto_stop(self, request_id):
        LOG.warning("hold timeout reached for %s; sending MOTION_STOP", request_id)
        self.motion.note_stop_requested(request_id)
        self._audit("auto_stop", {"request_id": request_id, "reason": "hold timeout"})
        try:
            response = self.vendor.send_command("MOTION_STOP", str(uuid.uuid4()))
        except VendorControlError as error:
            self.motion.mark_vendor_error(error)
            self._audit("auto_stop_error", {"request_id": request_id, "error": error.as_dict()})
            return
        self.motion.mark_stop_accepted(response, request_id)
        self._audit("auto_stop_response", {
            "request_id": request_id,
            "http_status": response.http_status,
            "latency_ms": response.latency_ms,
        })

    # -- response helpers ------------------------------------------------
    def _observation(self):
        try:
            return self.store.motion_observation()
        except Exception:  # pragma: no cover - store always provides this
            LOG.exception("cannot read motion observation")
            return {}

    def _observed_state(self):
        observation = self._observation()
        left = observation.get("left_wheel_speed")
        right = observation.get("right_wheel_speed")
        speed = None
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            speed = round((left + right) / 2.0, 2)
        return {
            "control_mode": observation.get("control_mode"),
            "gait_type": observation.get("gait_type"),
            "locomotion_mode": self.motion.locomotion_mode(observation),
            "wheel_speed_applicable": self.motion.wheel_speed_applicable(observation),
            "vehicle_speed": speed,
            "left_wheel_speed": left,
            "right_wheel_speed": right,
            "fall_state": observation.get("fall_state"),
        }

    def _current_phase(self, request_id):
        command = self.motion.command
        if command and command.get("request_id") == request_id:
            return command.get("phase")
        return None

    def _success_response(self, command_id, command_def, request_id, client_request_id,
                          response, extra=None):
        phase = self._current_phase(request_id)
        payload = {
            "accepted": True,
            "request_id": request_id,
            "operation_id": request_id,
            "client_request_id": client_request_id,
            "command_id": command_id,
            "vendor_command": command_def.get("vendor_command"),
            "vendor_response": response.to_dict(),
            "submitted_at": response.submitted_at,
            "latency_ms": response.latency_ms,
            "observed_state": self._observed_state(),
            "phase": phase,
            "execution_state": "completed" if phase == "completed" else "running",
            "completion_confirmed": phase == "completed",
            "error": None,
        }
        if extra:
            payload.update(extra)
        return payload

    def _error_response(self, command_id, command_def, request_id, client_request_id, error):
        return {
            "accepted": False,
            "request_id": request_id,
            "client_request_id": client_request_id,
            "command_id": command_id,
            "vendor_command": command_def.get("vendor_command"),
            "vendor_response": {
                "http_status": getattr(error, "http_status", None),
                "latency_ms": getattr(error, "latency_ms", None),
                "raw_body": getattr(error, "raw_body", ""),
            },
            "submitted_at": getattr(error, "submitted_at", time.time()),
            "latency_ms": getattr(error, "latency_ms", None),
            "observed_state": self._observed_state(),
            "phase": self._current_phase(request_id),
            "error": error.as_dict() if hasattr(error, "as_dict") else {
                "code": "vendor_error", "message": str(error)},
        }

    def _stop_success_response(self, request_id, response):
        return {
            "accepted": True,
            "request_id": request_id,
            "operation_id": request_id,
            "command_id": "motion_stop",
            "vendor_command": "MOTION_STOP",
            "vendor_response": response.to_dict(),
            "submitted_at": response.submitted_at,
            "latency_ms": response.latency_ms,
            "observed_state": self._observed_state(),
            "phase": "accepted",
            "execution_state": "stop_requested",
            "completion_confirmed": False,
            "error": None,
        }

    def _stop_error_response(self, request_id, error):
        return {
            "accepted": False,
            "request_id": request_id,
            "command_id": "motion_stop",
            "vendor_command": "MOTION_STOP",
            "vendor_response": {
                "http_status": getattr(error, "http_status", None),
                "latency_ms": getattr(error, "latency_ms", None),
                "raw_body": getattr(error, "raw_body", ""),
            },
            "submitted_at": getattr(error, "submitted_at", time.time()),
            "latency_ms": getattr(error, "latency_ms", None),
            "observed_state": self._observed_state(),
            "phase": self._current_phase(request_id),
            "error": error.as_dict() if hasattr(error, "as_dict") else {
                "code": "vendor_error", "message": str(error)},
        }

    # -- idempotency -----------------------------------------------------
    def _idempotent_lookup(self, key, command_id):
        if not key:
            return None
        self._prune_idempotency()
        entry = self._idempotency.get(key)
        if not entry:
            return None
        if entry["command_id"] != command_id:
            raise MotionControlError(
                "client_request_id was already used for a different command", 409,
                "idempotency_conflict")
        return entry["result"]

    def _idempotent_store(self, key, command_id, result):
        if not key:
            return
        self._prune_idempotency()
        if len(self._idempotency) >= IDEMPOTENCY_MAX:
            oldest = min(self._idempotency.items(), key=lambda item: item[1]["at"])
            self._idempotency.pop(oldest[0], None)
        self._idempotency[key] = {
            "at": time.monotonic(),
            "command_id": command_id,
            "result": dict(result),
        }

    def _prune_idempotency(self):
        now = time.monotonic()
        stale = [key for key, entry in self._idempotency.items()
                 if now - entry["at"] > IDEMPOTENCY_TTL]
        for key in stale:
            self._idempotency.pop(key, None)

    def _audit(self, event, detail):
        record = {"event": event}
        record.update(detail)
        self.audit.write(record)
