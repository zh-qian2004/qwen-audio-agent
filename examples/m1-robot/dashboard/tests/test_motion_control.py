import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from control_audit import ControlAudit
from motion_control import MotionControlError, MotionController
from motion_state import MotionState
from vendor_control import (
    VendorConnectionError,
    VendorControlClient,
    VendorHttpError,
    VendorProtocolError,
    VendorResponse,
    VendorTimeoutError,
)


COMMAND_CONFIG = json.loads((ROOT / "config" / "vendor_commands.json").read_text(encoding="utf-8"))


def vendor_response(request_id="test-request", http_status=200, body=None):
    now = time.time()
    return VendorResponse(
        request_id=request_id,
        method="POST",
        path="/api/control",
        submitted_at=now - 0.05,
        responded_at=now,
        latency_ms=50.0,
        http_status=http_status,
        content_type="application/json",
        body=body if body is not None else {"status": "success"},
        raw_body="{}",
    )


class FakeVendorServer:
    def __init__(self, delay=0.0, responder=None):
        self.delay = delay
        self.responder = responder
        self.requests = []
        self.httpd = None
        self.thread = None

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if owner.delay:
                    time.sleep(owner.delay)
                owner.requests.append({
                    "method": method,
                    "path": self.path,
                    "body": raw.decode("utf-8", errors="replace"),
                })
                if owner.responder:
                    status, content_type, payload = owner.responder(method, self.path, raw)
                else:
                    status, content_type, payload = owner.default_response(method, self.path)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    @property
    def port(self):
        return self.httpd.server_address[1]

    @staticmethod
    def default_response(method, path):
        if path == "/":
            return 200, "text/html", b"<html>vendor</html>"
        if path == "/api/battery":
            return 200, "application/json", json.dumps({"power": 63, "voltage": 26.1}).encode()
        if path == "/api/status":
            return 200, "application/json", b"null"
        return 200, "application/json", json.dumps({"status": "success"}).encode()


class VendorClientTests(unittest.TestCase):
    def test_control_success_records_http_and_latency(self):
        with FakeVendorServer() as server:
            client = VendorControlClient(port=server.port)
            response = client.send_command("MOVE_FORWARD")
        self.assertEqual(response.http_status, 200)
        self.assertEqual(response.body["status"], "success")
        self.assertGreaterEqual(response.latency_ms, 0)
        self.assertTrue(response.request_id)
        self.assertEqual(server.requests[-1]["path"], "/api/control")
        self.assertEqual(json.loads(server.requests[-1]["body"])["command"], "MOVE_FORWARD")

    def test_invalid_json_raises_protocol_error(self):
        with FakeVendorServer(responder=lambda m, p, b: (200, "application/json", b"{broken")) as server:
            client = VendorControlClient(port=server.port)
            with self.assertRaises(VendorProtocolError) as context:
                client.battery()
        self.assertIn("invalid JSON", context.exception.message)
        self.assertEqual(context.exception.http_status, 200)

    def test_http_error_is_normalized(self):
        with FakeVendorServer(responder=lambda m, p, b: (500, "application/json", b'{"error":"boom"}')) as server:
            client = VendorControlClient(port=server.port)
            with self.assertRaises(VendorHttpError) as context:
                client.send_command("MOVE_FORWARD")
        self.assertEqual(context.exception.http_status, 500)
        self.assertIn("HTTP 500", context.exception.message)

    def test_connection_failure_is_normalized(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = VendorControlClient(port=port, connect_timeout=0.2)
        # Windows may report a closed local port as a timeout instead of refused;
        # both are normalized into a VendorControlError subclass.
        with self.assertRaises((VendorConnectionError, VendorTimeoutError)):
            client.send_command("MOVE_FORWARD")

    def test_read_timeout_is_normalized(self):
        with FakeVendorServer(delay=0.6) as server:
            client = VendorControlClient(port=server.port, read_timeout=0.1)
            with self.assertRaises(VendorTimeoutError):
                client.send_command("MOVE_FORWARD")

    def test_emergency_and_reset_routes(self):
        with FakeVendorServer() as server:
            client = VendorControlClient(port=server.port)
            emergency = client.emergency_stop()
            reset = client.reset_emergency()
        self.assertEqual(emergency.body["status"], "success")
        self.assertEqual(reset.body["status"], "success")
        self.assertEqual([item["path"] for item in server.requests],
                         ["/api/emergency_stop", "/api/reset_emergency"])

    def test_health_accepts_html(self):
        with FakeVendorServer() as server:
            client = VendorControlClient(port=server.port)
            response = client.health()
        self.assertEqual(response.http_status, 200)
        self.assertIn("text/html", response.content_type)
        self.assertIn("vendor", response.raw_body)


class CommandConfigTests(unittest.TestCase):
    REQUIRED = {
        "MOVE_FORWARD", "MOVE_BACKWARD", "STRAFE_LEFT", "STRAFE_RIGHT",
        "ROTATE_LEFT", "ROTATE_RIGHT", "MOTION_STOP",
        "GET_UP", "GET_OFF_CAR", "BEND_WAIST", "STRAIGHTEN_WAIST",
        "VISUAL_GET_ON_CAR", "HEAD_UP", "HEAD_RESET", "WAVE_HAND", "HAND_RESET",
        "RETURN_TO_CHARGE", "ENTER_PRESENTATION_MODE", "SELF_INTRODUCTION",
        "ME_TOO_HAPPY", "SET_MODE_COMMAND-ONLY", "SET_MODE_VOICE-ONLY",
        "SET_MODE_MIXED", "EMERGENCY_STOP",
    }
    FRONTEND_ONLY = {"SAY_WOZAI", "MUTE", "RESET", "ENTER-FOLLOW", "REMOTE_CONTROL_MODE"}
    DISABLED_BUSINESS = {
        "enter_presentation_mode", "self_introduction", "me_too_happy",
    }

    def setUp(self):
        self.commands = COMMAND_CONFIG["commands"]

    def test_required_vendor_commands_are_registered(self):
        registered = {item["vendor_command"] for item in self.commands}
        self.assertTrue(self.REQUIRED.issubset(registered), self.REQUIRED - registered)

    def test_frontend_observed_commands_are_marked(self):
        by_vendor = {item["vendor_command"]: item for item in self.commands}
        for command in self.FRONTEND_ONLY:
            self.assertIn(command, by_vendor)
            self.assertEqual(by_vendor[command]["verification_state"], "frontend_observed")

    def test_unverified_business_commands_are_disabled(self):
        by_id = {item["id"]: item for item in self.commands}
        for command_id in self.DISABLED_BUSINESS:
            self.assertIn(command_id, by_id)
            self.assertFalse(by_id[command_id]["dashboard_enabled"])
            self.assertFalse(by_id[command_id]["hardware_verified"])

    def test_only_formal_mount_and_dismount_workflows_are_exposed(self):
        enabled = [item for item in self.commands if item.get("dashboard_enabled", True)]
        mount = [item for item in enabled if item.get("vehicle_transition") == "mount"]
        dismount = [item for item in enabled if item.get("vehicle_transition") == "dismount"]
        self.assertEqual([(item["id"], item["vendor_command"]) for item in mount],
                         [("visual_get_on_car", "VISUAL_GET_ON_CAR")])
        self.assertEqual([(item["id"], item["vendor_command"]) for item in dismount],
                         [("get_off_car", "GET_OFF_CAR")])
        self.assertNotIn("GET_ON_CAR", {item["vendor_command"] for item in self.commands})

    def test_charge_workflows_use_authoritative_completion_signals(self):
        by_id = {item["id"]: item for item in self.commands}
        self.assertTrue(by_id["return_to_charge"]["dashboard_enabled"])
        self.assertTrue(by_id["off_charger"]["dashboard_enabled"])
        self.assertIn("/ControlGoChargeDone_topic",
                      by_id["return_to_charge"]["feedback_sources"])
        self.assertEqual(by_id["off_charger"]["transport"], "business_app_topic")
        self.assertEqual(
            by_id["off_charger"]["business_request"]["instruction"][0]["name"],
            "off-charger",
        )
        for command_id in ("visual_get_on_car", "get_off_car", "return_to_charge", "off_charger"):
            self.assertIn("completion_signal", by_id[command_id])
            self.assertIn("event", by_id[command_id]["completion_signal"])
            self.assertIn("state", by_id[command_id]["completion_signal"])

    def test_required_fields_and_invariants(self):
        ids = [item["id"] for item in self.commands]
        self.assertEqual(len(ids), len(set(ids)))
        vendor_commands = {item["vendor_command"] for item in self.commands}
        for item in self.commands:
            for field in ("id", "vendor_command", "display_name", "category", "physical_effect",
                          "interaction", "stop_command", "confirmation", "timeout_seconds",
                          "verification_state", "feedback_sources"):
                self.assertIn(field, item, f"{item.get('id')} missing {field}")
            self.assertIn(item["interaction"], {"hold", "trigger", "toggle", "workflow"})
            self.assertIn(item["verification_state"], {"vendor_confirmed", "frontend_observed"})
            self.assertIn(item["confirmation"], {"none", "confirm"})
            self.assertGreater(item["timeout_seconds"], 0)
            self.assertEqual(set(item["physical_effect"]), {"head", "arms", "legs", "base"})
            if item["stop_command"]:
                self.assertIn(item["stop_command"], vendor_commands)
            if item["category"] == "direction" and item["vendor_command"].startswith(("MOVE_", "STRAFE_", "ROTATE_")):
                self.assertEqual(item["interaction"], "hold")
                self.assertEqual(item["stop_command"], "MOTION_STOP")
        direction = [item for item in self.commands if item["category"] == "direction"]
        self.assertEqual(len(direction), 7)

    def test_meta_has_labels_and_limits(self):
        meta = COMMAND_CONFIG["meta"]
        self.assertEqual(meta["vendor_base_url"], "http://127.0.0.1:12321")
        self.assertIn("53", meta["gait_labels"])
        self.assertIn("0", meta["control_mode_labels"])
        self.assertGreater(meta["max_hold_seconds"], 0)


class MotionStateTests(unittest.TestCase):
    def setUp(self):
        self.state = MotionState(command_timeout_default=5)
        self.definition = next(item for item in COMMAND_CONFIG["commands"] if item["id"] == "move_forward")

    @staticmethod
    def observation(**overrides):
        base = {
            "control_mode": 0,
            "gait_type": 53,
            "linear_velocity_x": 0,
            "angular_rate_z": 0,
            "fall_state": 0,
            "left_wheel_speed": 0,
            "right_wheel_speed": 0,
            "head_yaw": 0.0,
            "head_pitch": 0.0,
            "joint_angles": [0.0] * 20,
            "ages": {"motion_feedback": 0.1, "self_fall": 0.1, "vehicle": 0.1, "head": 0.1, "kinematic": 0.1},
        }
        base.update(overrides)
        return base

    def begin(self, command_id="move_forward", request_id="req-1"):
        definition = next(item for item in COMMAND_CONFIG["commands"] if item["id"] == command_id)
        self.state.update_observation(self.observation(), now=0.0)
        return definition, self.state.begin(definition, request_id, str(uuid.uuid4()), "test", now_mono=0.0)

    def test_full_hold_lifecycle(self):
        definition, command = self.begin()
        self.state.mark_sending()
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.assertEqual(self.state.command["phase"], "observing")
        self.state.tick(self.observation(left_wheel_speed=220, right_wheel_speed=210, control_mode=1, gait_type=7), now_mono=0.2)
        self.assertEqual(self.state.command["phase"], "observed_running")
        self.assertIsNotNone(self.state.command["motion_observed_at"])
        self.state.tick(self.observation(control_mode=1, gait_type=7), now_mono=0.3)
        self.state.tick(self.observation(control_mode=1, gait_type=7), now_mono=1.2)
        self.assertEqual(self.state.command["phase"], "observed_stopped")
        self.state.tick(self.observation(control_mode=1, gait_type=7), now_mono=1.3)
        self.assertEqual(self.state.command["phase"], "completed")
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["command"]["phase"], "completed")
        self.assertEqual(snapshot["robot"]["control_mode"], 1)
        self.assertEqual(snapshot["robot"]["gait_type_label"], "中步幅前进")

    def test_workflow_waits_for_business_event_and_hardware_state(self):
        definition = next(item for item in COMMAND_CONFIG["commands"]
                          if item["id"] == "visual_get_on_car")
        initial = self.observation(is_on_car=0, is_on_charger=0, is_charging=False,
                                   business_event=None)
        self.state.update_observation(initial, now=0.0)
        self.state.begin(definition, "req-1", str(uuid.uuid4()), "test", now_mono=0.0)
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)

        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False,
            business_event=None, control_mode=2, gait_type=0,
        ), now_mono=10.0)
        self.assertNotEqual(self.state.command["phase"], "completed")

        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False,
            business_event={
                "type": "get_on_off_car_status", "action": "get_on_car",
                "status": "success", "received_at": time.time() + 1,
            },
        ), now_mono=11.0)
        self.assertEqual(self.state.command["phase"], "completed")
        self.assertEqual(self.state.command["observation_result"],
                         "business_and_hardware_confirmed")

    def test_workflow_success_event_without_hardware_state_does_not_complete(self):
        definition = next(item for item in COMMAND_CONFIG["commands"]
                          if item["id"] == "return_to_charge")
        self.state.update_observation(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
        ), now=0.0)
        self.state.begin(definition, "req-1", str(uuid.uuid4()), "test", now_mono=0.0)
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False,
            business_event={
                "type": "on_off_charger_status", "action": "on_charger",
                "status": "success", "received_at": time.time() + 1,
            },
        ), now_mono=35.0)
        self.assertNotEqual(self.state.command["phase"], "completed")

    def test_workflow_current_business_failure_releases_command_slot(self):
        definition = next(item for item in COMMAND_CONFIG["commands"]
                          if item["id"] == "return_to_charge")
        self.state.update_observation(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
        ), now=0.0)
        self.state.begin(definition, "req-1", str(uuid.uuid4()), "test", now_mono=0.0)
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False,
            business_event={
                "type": "on_off_charger_status", "action": "on_charger",
                "status": "failed", "reason": "charge_failed",
                "received_at": time.time() + 1,
            },
        ), now_mono=1.0)
        self.assertEqual(self.state.command["phase"], "state_mismatch")
        self.assertFalse(self.state.is_busy())

    def test_workflow_stale_business_failure_is_ignored(self):
        definition = next(item for item in COMMAND_CONFIG["commands"]
                          if item["id"] == "return_to_charge")
        self.state.update_observation(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
        ), now=0.0)
        self.state.begin(definition, "req-1", str(uuid.uuid4()), "test", now_mono=0.0)
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False,
            business_event={
                "type": "on_off_charger_status", "action": "on_charger",
                "status": "failed", "reason": "old_failure", "received_at": 1.0,
            },
        ), now_mono=1.0)
        self.assertTrue(self.state.is_busy())

    def test_go_charge_done_failure_releases_command_slot(self):
        definition = next(item for item in COMMAND_CONFIG["commands"]
                          if item["id"] == "return_to_charge")
        self.state.update_observation(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
        ), now=0.0)
        self.state.begin(definition, "req-1", str(uuid.uuid4()), "test", now_mono=0.0)
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
            go_charge_result={"value": 0, "received_at": time.time() + 1},
        ), now_mono=1.0)
        self.assertEqual(self.state.command["phase"], "state_mismatch")
        self.assertFalse(self.state.is_busy())

    def test_go_charge_work_mode_return_to_idle_fails_after_grace(self):
        definition = next(item for item in COMMAND_CONFIG["commands"]
                          if item["id"] == "return_to_charge")
        self.state.update_observation(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
            business_work_mode="idle",
        ), now=0.0)
        self.state.begin(definition, "req-1", str(uuid.uuid4()), "test", now_mono=0.0)
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
            business_work_mode="go_charge",
        ), now_mono=1.0)
        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
            business_work_mode="idle",
        ), now_mono=2.0)
        self.assertTrue(self.state.is_busy())
        self.state.tick(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
            business_work_mode="idle",
        ), now_mono=5.1)
        self.assertEqual(self.state.command["phase"], "state_mismatch")
        self.assertFalse(self.state.is_busy())

    def test_no_observation_times_out(self):
        definition, command = self.begin()
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.tick(self.observation(), now_mono=8.5)
        self.assertEqual(self.state.command["phase"], "timed_out")
        self.assertEqual(self.state.command["error"]["code"], "timed_out")

    def test_slow_walking_accumulates_joint_change_from_command_baseline(self):
        definition = next(item for item in COMMAND_CONFIG["commands"] if item["id"] == "move_forward")
        self.state.update_observation(self.observation(control_mode=1, gait_type=0), now=0.0)
        self.state.begin(definition, "req-1", str(uuid.uuid4()), "test", now_mono=0.0)
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        for index, angle in enumerate((0.6, 1.2, 1.8, 2.4), start=1):
            joints = [0.0] * 20
            joints[0] = angle
            self.state.tick(
                self.observation(control_mode=1, gait_type=0, joint_angles=joints),
                now_mono=index * 0.1,
            )
        self.assertEqual(self.state.command["phase"], "observed_running")
        self.assertEqual(self.state.command["observation_result"], "observed_moving")
        self.assertEqual(self.state.command["locomotion_mode_observed"], "walking")
        self.assertTrue(any("joint moved" in item for item in self.state.command["motion_evidence"]))

    def test_wheel_speed_is_not_applicable_in_walking_mode(self):
        self.state.update_observation(
            self.observation(control_mode=1, gait_type=1, left_wheel_speed=0, right_wheel_speed=0),
            now=0.0,
        )
        robot = self.state.snapshot()["robot"]
        self.assertEqual(robot["locomotion_mode"], "walking")
        self.assertFalse(robot["wheel_speed_applicable"])

    def test_nonzero_wheel_feedback_selects_wheeled_mode(self):
        self.state.update_observation(
            self.observation(control_mode=1, left_wheel_speed=120, right_wheel_speed=118),
            now=0.0,
        )
        robot = self.state.snapshot()["robot"]
        self.assertEqual(robot["locomotion_mode"], "wheeled")
        self.assertTrue(robot["wheel_speed_applicable"])

    def test_hold_deadline_exposes_request(self):
        definition, command = self.begin()
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.assertEqual(self.state.hold_expired(now_mono=1.0), None)
        self.assertEqual(self.state.hold_expired(now_mono=6.0), "req-1")
        self.state.note_stop_requested("stop-1", now_mono=6.0)
        self.assertEqual(self.state.hold_expired(now_mono=7.0), None)

    def test_requested_hold_duration_controls_dashboard_deadline(self):
        definition = next(item for item in COMMAND_CONFIG["commands"] if item["id"] == "move_forward")
        self.state.update_observation(self.observation(), now=0.0)
        command = self.state.begin(
            definition, "req-1", str(uuid.uuid4()), "test",
            now_mono=0.0, hold_seconds=0.25,
        )
        self.assertEqual(command["hold_duration_seconds"], 0.25)
        self.assertGreater(command["timeout_seconds"], command["hold_duration_seconds"])
        self.assertEqual(self.state.hold_expired(now_mono=0.2), None)
        self.assertEqual(self.state.hold_expired(now_mono=0.3), "req-1")

    def test_stop_before_motion_completes(self):
        definition, _ = self.begin()
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.mark_stop_accepted(vendor_response("stop-1"), now_mono=0.1)
        self.assertEqual(self.state.command["phase"], "completed")
        self.assertEqual(self.state.command["observation_result"], "not_confirmed")

    def test_stop_cancels_workflow_and_releases_command_slot(self):
        definition = next(item for item in COMMAND_CONFIG["commands"]
                          if item["id"] == "return_to_charge")
        self.state.update_observation(self.observation(
            is_on_car=1, is_on_charger=0, is_charging=False, business_event=None,
        ), now=0.0)
        self.state.begin(definition, "req-1", str(uuid.uuid4()), "test", now_mono=0.0)
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.mark_stop_accepted(vendor_response("stop-1"), now_mono=0.1)
        self.assertEqual(self.state.command["phase"], "cancelled")
        self.assertFalse(self.state.is_busy())

    def test_fall_state_aborts_command(self):
        definition, _ = self.begin()
        self.state.mark_vendor_accepted(vendor_response("req-1"), now_mono=0.0)
        self.state.tick(self.observation(fall_state=1), now_mono=0.5)
        self.assertEqual(self.state.command["phase"], "state_mismatch")
        self.assertIn("fall state", self.state.command["error"]["message"])

    def test_snapshot_has_required_shape(self):
        snapshot = self.state.snapshot()
        self.assertIn("vendor_service", snapshot)
        self.assertIn("command", snapshot)
        self.assertIn("robot", snapshot)
        for key in ("control_mode", "gait_type", "fall_state", "left_wheel_speed",
                    "right_wheel_speed", "head_yaw", "head_pitch"):
            self.assertIn(key, snapshot["robot"])


class FakeStore:
    def __init__(self, observation=None):
        self.observation = observation or {
            "control_mode": 0,
            "gait_type": 53,
            "linear_velocity_x": 0,
            "angular_rate_z": 0,
            "fall_state": 0,
            "left_wheel_speed": 0,
            "right_wheel_speed": 0,
            "head_yaw": 0.0,
            "head_pitch": 0.0,
            "joint_angles": [0.0] * 20,
            "ages": {},
        }

    def motion_observation(self):
        return dict(self.observation)


class StubVendor:
    def __init__(self):
        self.calls = []
        self.emergency_error = None

    def send_command(self, command, request_id=None):
        self.calls.append(("command", command))
        return vendor_response(request_id or "stub", body={"status": "success", "command": command})

    def emergency_stop(self, request_id=None):
        self.calls.append(("emergency_stop", request_id))
        if self.emergency_error:
            raise self.emergency_error
        return vendor_response(request_id or "stub")

    def reset_emergency(self, request_id=None):
        self.calls.append(("reset_emergency", request_id))
        return vendor_response(request_id or "stub")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.audit = ControlAudit(os.path.join(self.tempdir.name, "audit.jsonl"))
        self.store = FakeStore()
        self.vendor = StubVendor()
        self.controller = MotionController(COMMAND_CONFIG, self.store, self.audit, vendor_client=self.vendor)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_idempotency_replays_without_second_vendor_call(self):
        key = str(uuid.uuid4())
        first = self.controller.command("head_reset", key, "test", "unit")
        second = self.controller.command("head_reset", key, "test", "unit")
        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(len([call for call in self.vendor.calls if call[0] == "command"]), 1)

    def test_busy_rejects_second_motion_command(self):
        self.controller.command("move_forward", str(uuid.uuid4()), "test", "unit")
        with self.assertRaises(MotionControlError) as context:
            self.controller.command("move_backward", str(uuid.uuid4()), "test", "unit")
        self.assertEqual(context.exception.status_code, 409)

    def test_stop_uses_dedicated_path_while_busy(self):
        self.controller.command("move_forward", str(uuid.uuid4()), "test", "unit")
        result = self.controller.stop("test stop", "unit")
        self.assertTrue(result["accepted"])
        self.assertIn(("command", "MOTION_STOP"), self.vendor.calls)

    def test_hold_duration_is_owned_by_dashboard(self):
        result = self.controller.command(
            "move_forward", str(uuid.uuid4()), "test", "unit", duration_seconds=0.25,
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["operation_id"], result["request_id"])
        self.assertEqual(result["duration_ms"], 250)
        self.assertEqual(result["execution_state"], "running")
        self.assertFalse(result["completion_confirmed"])
        self.assertAlmostEqual(self.controller.motion.command["hold_duration_seconds"], 0.25)
        self.assertGreater(
            self.controller.motion.command["timeout_seconds"],
            self.controller.motion.command["hold_duration_seconds"],
        )

    def test_duration_is_rejected_for_non_hold_actions(self):
        with self.assertRaises(MotionControlError) as context:
            self.controller.command(
                "wave_hand", str(uuid.uuid4()), "test", "unit", duration_seconds=1.0,
            )
        self.assertEqual(context.exception.code, "duration_not_supported")

    def test_hold_duration_respects_safety_bounds(self):
        with self.assertRaises(MotionControlError) as context:
            self.controller.command(
                "move_forward", str(uuid.uuid4()), "test", "unit", duration_seconds=0.1,
            )
        self.assertEqual(context.exception.code, "invalid_duration")

    def test_disabled_frontend_command_is_rejected(self):
        with self.assertRaises(MotionControlError) as context:
            self.controller.command("enter_follow", str(uuid.uuid4()), "test", "unit")
        self.assertEqual(context.exception.status_code, 403)

    def test_disabled_business_command_is_rejected(self):
        with self.assertRaises(MotionControlError) as context:
            self.controller.command("self_introduction", str(uuid.uuid4()), "test", "unit")
        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.code, "command_disabled")

    def test_off_charger_uses_business_topic_transport(self):
        published = []
        self.controller.business_publisher = lambda payload: (
            published.append(payload) or json.dumps(payload)
        )
        self.store.observation.update({
            "is_on_car": 1, "is_on_charger": 1, "is_charging": True,
        })
        result = self.controller.command("off_charger", str(uuid.uuid4()), "test", "unit")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["vendor_response"]["path"], "/BusinessAppReq_topic")
        self.assertEqual(published[0]["instruction"][0]["name"], "off-charger")

    def test_charge_preconditions_reject_wrong_state(self):
        self.store.observation.update({
            "is_on_car": 0, "is_on_charger": 0, "is_charging": False,
        })
        with self.assertRaises(MotionControlError) as context:
            self.controller.command("return_to_charge", str(uuid.uuid4()), "test", "unit")
        self.assertEqual(context.exception.code, "precondition_failed")

    def test_fall_gate_blocks_base_motion(self):
        self.store.observation["fall_state"] = 1
        with self.assertRaises(MotionControlError) as context:
            self.controller.command("move_forward", str(uuid.uuid4()), "test", "unit")
        self.assertEqual(context.exception.code, "fall_detected")
        allowed = self.controller.command("get_up", str(uuid.uuid4()), "test", "unit")
        self.assertTrue(allowed["accepted"])

    def test_emergency_falls_back_to_control_endpoint(self):
        self.vendor.emergency_error = VendorHttpError(
            "not found", request_id="e1", submitted_at=time.time(), responded_at=time.time(),
            latency_ms=1.0, http_status=404, raw_body="not found",
        )
        result = self.controller.emergency_stop("test", "unit")
        self.assertTrue(result["accepted"])
        self.assertIn("fallback", result["route"])
        self.assertTrue(self.controller.motion.emergency["active"])
        self.assertIn(("command", "EMERGENCY_STOP"), self.vendor.calls)

    def test_capabilities_exposes_all_commands(self):
        capabilities = self.controller.capabilities()
        total = sum(len(category["commands"]) for category in capabilities["categories"])
        self.assertEqual(total, len(COMMAND_CONFIG["commands"]))
        self.assertEqual(capabilities["base_url"], "http://127.0.0.1:12321")

    def test_audit_records_are_written(self):
        self.controller.command("head_reset", str(uuid.uuid4()), "audit test", "unit")
        records = self.audit.tail(20)
        self.assertTrue(any(record["event"] == "command_request" for record in records))
        self.assertTrue(any(record["event"] == "vendor_response" for record in records))


if __name__ == "__main__":
    unittest.main(verbosity=2)
