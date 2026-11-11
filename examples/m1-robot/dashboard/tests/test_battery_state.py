import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from state import StateStore


CONFIG = json.loads((ROOT / "config" / "dashboard.json").read_text(encoding="utf-8"))


class BatteryStateTest(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(CONFIG)
        self.store.history["battery"].clear()

    def test_estimates_current_charging_segment(self):
        base = 1_000.0
        self.store.history["battery"].extend([
            {"t": base - 60, "power": 15},
            {"t": base, "power": 10},
            {"t": base + 180, "power": 11},
            {"t": base + 360, "power": 12},
            {"t": base + 540, "power": 13},
            {"t": base + 720, "power": 14},
        ])
        self.store.data["battery"] = {"power": 14, "is_charging": True}

        status = self.store.battery_status()

        self.assertEqual(status["charging_rate_percent_per_hour"], 20.0)
        self.assertEqual(status["soc_delta"], 4.0)
        self.assertEqual(status["duration_minutes"], 12.0)
        self.assertEqual(status["eta_30_minutes"], 48.0)
        self.assertEqual(status["eta_full_minutes"], 258.0)

    def test_does_not_report_rate_when_not_charging(self):
        self.store.history["battery"].extend([
            {"t": 1_000.0, "power": 10},
            {"t": 1_180.0, "power": 11},
        ])
        self.store.data["battery"] = {"power": 11, "is_charging": False}

        self.assertIsNone(self.store.battery_status()["charging_rate_percent_per_hour"])


if __name__ == "__main__":
    unittest.main()
