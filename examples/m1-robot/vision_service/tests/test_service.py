import json
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import VisionService


class FakeRuntime:
    def __init__(self, _ros_config, _processor_config, on_result):
        self.on_result = on_result
        self.available = False
        self.flags = []

    def start(self):
        self.available = True

    def stop(self):
        self.available = False

    def set_detection_enabled(self, enabled):
        self.flags.append(enabled)

    def status(self):
        return {"available": self.available}

    def emit(self, payload, now=1.0):
        self.on_result(json.dumps(payload), 1000 + now, now)


def config():
    return {
        "service": {"version": "test", "history_size": 10},
        "ros": {},
        "processors": {
            "object_detection": {
                "enabled": True,
                "auto_start": False,
                "manage_source_enable": False,
                "probe_existing_stream_seconds": 0,
                "source_stale_after_seconds": 1,
                "warmup_seconds": 1,
                "confidence_threshold": 0.5,
                "result_topic": "/result",
                "enable_topic": "/flag",
                "model": "test.hbm",
            },
            "person_tracking": {
                "enabled": True,
                "auto_start": True,
                "min_confirmed_hits": 1,
                "max_missed_frames": 2,
            },
        },
    }


class VisionServiceTests(unittest.TestCase):
    def test_person_only_detection_flows_into_tracking_bus(self):
        service = VisionService(config(), runtime_factory=FakeRuntime)
        service.start()
        service.start_processor("object_detection")
        service.runtime.emit({
            "frame_seq": 7,
            "model_input_width": 960,
            "model_input_height": 544,
            "body_count": 1,
            "body_boxes": [{"confidence": 0.9, "x_offset": 96, "y_offset": 54.4, "width": 192, "height": 326.4}],
            "head_count": 1,
            "head_boxes": [{"confidence": 0.9, "x_offset": 120, "y_offset": 60, "width": 60, "height": 60}],
        })
        detection = service.detection_bus.latest_result()
        tracking = service.bus.latest_result()
        self.assertEqual([item["label"] for item in detection["detections"]], ["person"])
        self.assertEqual(tracking["type"], "tracking_frame")
        self.assertEqual(tracking["tracks"][0]["track_id"], 1)
        self.assertEqual(len(service.processor_statuses()), 2)
        service.stop()

    def test_starting_detection_restarts_stopped_tracker(self):
        service = VisionService(config(), runtime_factory=FakeRuntime)
        service.start()
        service.stop_processor("person_tracking")
        service.start_processor("object_detection")
        states = {item["id"]: item["state"] for item in service.processor_statuses()}
        self.assertEqual(states["person_tracking"], "running")
        service.stop()


if __name__ == "__main__":
    unittest.main()
