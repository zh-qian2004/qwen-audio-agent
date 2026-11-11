import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system_monitor import _bpu_metrics


class BPUMetricsTest(unittest.TestCase):
    def test_reads_utilization_frequency_and_unique_users(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "bpu0"
            devfreq = core / "devfreq" / "3a000000.bpu"
            devfreq.mkdir(parents=True)
            (root / "core_num").write_text("1\n")
            (root / "ratio").write_text("37\n")
            (root / "users").write_text(
                "*User via BPU Bus*\nuser ratio\n1611 12\n1613 5\n"
                "*User via BPU Core(0)*\n1611 20\n1613 5\n"
            )
            (core / "ratio").write_text("37\n")
            (devfreq / "cur_freq").write_text("996000000\n")
            (devfreq / "max_freq").write_text("1000000000\n")

            metrics = _bpu_metrics(root)

        self.assertTrue(metrics["available"])
        self.assertEqual(metrics["core_count"], 1)
        self.assertEqual(metrics["utilization_percent"], 37.0)
        self.assertEqual(metrics["frequency_hz"], 996000000)
        self.assertEqual(metrics["max_frequency_hz"], 1000000000)
        self.assertEqual(metrics["user_count"], 2)
        self.assertEqual(metrics["users"][0], {"pid": 1611, "utilization_percent": 20.0})

    def test_missing_sysfs_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics = _bpu_metrics(Path(directory))

        self.assertFalse(metrics["available"])
        self.assertIsNone(metrics["utilization_percent"])
        self.assertEqual(metrics["user_count"], 0)


if __name__ == "__main__":
    unittest.main()
