import sys
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from result_bus import ResultBus


class ResultBusTests(unittest.TestCase):
    def test_latest_history_and_sequence_are_bounded(self):
        bus = ResultBus(history_size=2)
        bus.publish({"value": 1, "received_monotonic": time.monotonic()})
        bus.publish({"value": 2, "received_monotonic": time.monotonic()})
        bus.publish({"value": 3, "received_monotonic": time.monotonic()})
        self.assertEqual(bus.latest_result()["value"], 3)
        self.assertEqual([item["value"] for item in bus.recent(10)], [2, 3])
        self.assertEqual(bus.status()["latest_sequence"], 3)

    def test_wait_for_update_wakes_without_queueing_every_event(self):
        bus = ResultBus()
        result = []

        def wait():
            result.append(bus.wait_for_update(0, timeout=1))

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.02)
        bus.publish({"value": 7, "received_monotonic": time.monotonic()})
        thread.join(1)
        self.assertEqual(result[0]["value"], 7)


if __name__ == "__main__":
    unittest.main()
