import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from processors.person_tracking import PersonTrackingProcessor, _iou
from result_bus import ResultBus


def frame(sequence, boxes, now):
    detections = []
    for index, (x1, y1, x2, y2) in enumerate(boxes):
        detections.append({
            "id": f"{sequence}:body:{index}",
            "class_id": 0,
            "label": "person",
            "source_label": "body",
            "confidence": 0.9,
            "bbox_normalized": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
        })
    return {
        "type": "detection_frame",
        "processor": "object_detection",
        "model": "test.hbm",
        "source_frame_sequence": sequence,
        "source_timestamp": None,
        "received_at": 1000 + now,
        "received_monotonic": now,
        "source_width": 960,
        "source_height": 544,
        "detections": detections,
    }


class PersonTrackingTests(unittest.TestCase):
    def config(self, **overrides):
        config = {
            "enabled": True,
            "auto_start": True,
            "iou_threshold": 0.2,
            "center_distance_threshold": 0.18,
            "smoothing_alpha": 0.65,
            "velocity_alpha": 0.5,
            "min_confirmed_hits": 2,
            "max_missed_frames": 2,
        }
        config.update(overrides)
        return config

    def test_iou(self):
        left = {"x1": 0, "y1": 0, "x2": 0.5, "y2": 0.5}
        right = {"x1": 0.25, "y1": 0.25, "x2": 0.75, "y2": 0.75}
        self.assertAlmostEqual(_iou(left, left), 1.0)
        self.assertAlmostEqual(_iou(left, right), 1 / 7)

    def test_moving_person_keeps_id_and_becomes_confirmed(self):
        bus = ResultBus()
        tracker = PersonTrackingProcessor(self.config(), bus)
        tracker.start()
        first = tracker.process_frame(frame(1, [(0.1, 0.1, 0.4, 0.8)], 1.0))
        second = tracker.process_frame(frame(2, [(0.12, 0.1, 0.42, 0.8)], 1.1))
        self.assertEqual(first["tracks"][0]["track_id"], second["tracks"][0]["track_id"])
        self.assertEqual(second["tracks"][0]["track_state"], "confirmed")
        self.assertEqual(second["events"][0]["type"], "person_entered")
        self.assertGreater(second["tracks"][0]["velocity"]["x"], 0)

    def test_two_people_keep_distinct_ids_when_order_changes(self):
        bus = ResultBus()
        tracker = PersonTrackingProcessor(self.config(min_confirmed_hits=1), bus)
        tracker.start()
        first = tracker.process_frame(frame(1, [
            (0.05, 0.1, 0.25, 0.8), (0.65, 0.1, 0.9, 0.8),
        ], 1.0))
        second = tracker.process_frame(frame(2, [
            (0.63, 0.1, 0.88, 0.8), (0.07, 0.1, 0.27, 0.8),
        ], 1.1))
        first_left_id = min(first["tracks"], key=lambda item: item["bbox_normalized"]["x1"])["track_id"]
        second_left_id = min(second["tracks"], key=lambda item: item["bbox_normalized"]["x1"])["track_id"]
        self.assertEqual(first_left_id, second_left_id)
        self.assertEqual(len({item["track_id"] for item in second["tracks"]}), 2)

    def test_lost_track_is_removed_and_emits_left(self):
        tracker = PersonTrackingProcessor(self.config(min_confirmed_hits=1), ResultBus())
        tracker.start()
        tracker.process_frame(frame(1, [(0.1, 0.1, 0.4, 0.8)], 1.0))
        lost = tracker.process_frame(frame(2, [], 1.1))
        self.assertEqual(lost["tracks"][0]["track_state"], "lost")
        tracker.process_frame(frame(3, [], 1.2))
        removed = tracker.process_frame(frame(4, [], 1.3))
        self.assertEqual(removed["track_count"], 0)
        self.assertEqual(removed["events"][0]["type"], "person_left")

    def test_stop_clears_tracks_and_ignores_frames(self):
        tracker = PersonTrackingProcessor(self.config(), ResultBus())
        tracker.start()
        tracker.process_frame(frame(1, [(0.1, 0.1, 0.4, 0.8)], time.monotonic()))
        tracker.stop()
        self.assertEqual(tracker.status()["active_tracks"], 0)
        self.assertIsNone(tracker.process_frame(frame(2, [], time.monotonic())))


if __name__ == "__main__":
    unittest.main()
