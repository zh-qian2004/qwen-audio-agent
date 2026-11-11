import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from state import StateStore


CONFIG = json.loads((ROOT / "config" / "dashboard.json").read_text(encoding="utf-8"))


class VehicleStateTest(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(CONFIG)

    def test_reports_unknown_mount_without_explicit_signal(self):
        status = self.store.vehicle_status()

        self.assertFalse(status["available"])
        self.assertEqual(status["status"], "no_data")
        self.assertEqual(status["mount"]["state"], "unknown")
        self.assertFalse(status["mount"]["known"])
        self.assertIsNone(status["wheel_motion"]["moving"])

    def test_normalizes_vehicle_feedback_without_inferring_mount(self):
        self.store.update_vehicle([0, 101, 102, 3, 0, 0, 7, 1, 11, 12])

        status = self.store.vehicle_status()

        self.assertTrue(status["available"])
        self.assertEqual(status["status"], "online")
        self.assertTrue(status["communication"]["feedback_online"])
        self.assertEqual(status["communication"]["raw"], 1)
        self.assertEqual(status["wheel_motion"]["state"], "moving")
        self.assertTrue(status["wheel_motion"]["moving"])
        self.assertTrue(status["faults"]["active"])
        self.assertEqual(status["faults"]["right_raw"], 7)
        self.assertEqual(status["mount"]["state"], "unknown")

    def test_zero_wheel_feedback_is_stopped_not_off_vehicle(self):
        self.store.update_vehicle([0, 0, 0, 0, 0, 0, 0, 1, 0, 0])

        status = self.store.vehicle_status()

        self.assertEqual(status["wheel_motion"]["state"], "stopped")
        self.assertFalse(status["wheel_motion"]["moving"])
        self.assertFalse(status["faults"]["active"])
        self.assertFalse(status["mount"]["known"])

    def test_business_status_is_authoritative_for_mount_state(self):
        self.store.update_business_report(json.dumps({
            "type": "system_status",
            "payload": {"data": {
                "is_on_car": 1,
                "is_on_charger": 1,
                "is_charging": 1,
                "work_mode": "idle",
                "stand_status": 2,
            }},
        }))
        status = self.store.vehicle_status()
        observation = self.store.motion_observation()

        self.assertTrue(status["mount"]["known"])
        self.assertEqual(status["mount"]["state"], "on_vehicle")
        self.assertEqual(status["mount"]["source"], "/BusinessAppRpt_topic")
        self.assertEqual(observation["is_on_car"], 1)
        self.assertEqual(observation["is_on_charger"], 1)

    def test_business_completion_event_is_retained(self):
        self.store.update_business_report(json.dumps({
            "type": "on_off_charger_status",
            "payload": {"action": "off_charger", "status": "success", "reason": ""},
        }))
        event = self.store.motion_observation()["business_event"]
        self.assertEqual(event["type"], "on_off_charger_status")
        self.assertEqual(event["action"], "off_charger")
        self.assertEqual(event["status"], "success")
        self.assertIsInstance(event["received_at"], float)

    def test_go_charge_result_is_retained_with_timestamp(self):
        self.store.update_go_charge_result(0)
        result = self.store.motion_observation()["go_charge_result"]
        self.assertEqual(result["value"], 0)
        self.assertIsInstance(result["received_at"], float)


if __name__ == "__main__":
    unittest.main()
