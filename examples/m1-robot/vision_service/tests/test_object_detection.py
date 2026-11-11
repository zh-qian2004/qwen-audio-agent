import json
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from processors.object_detection import ObjectDetectionProcessor, parse_multitask_result
from result_bus import ResultBus


SAMPLE = {
    "frame_seq": 42,
    "model_input_width": 960,
    "model_input_height": 544,
    "body_count": 2,
    "body_boxes": [
        {"confidence": 0.91, "x_offset": 10, "y_offset": 20, "width": 100, "height": 200},
        {"confidence": 0.2, "x_offset": 0, "y_offset": 0, "width": 10, "height": 10},
    ],
    "head_count": 1,
    "head_boxes": [
        {"confidence": 0.8, "x_offset": 930, "y_offset": 500, "width": 80, "height": 80},
    ],
    "face_count": 0,
    "face_boxes": [],
    "hand_count": 0,
    "hand_boxes": [],
    "keypoints_count": 1,
}


class FakeRuntime:
    def __init__(self):
        self.flags = []

    def set_detection_enabled(self, enabled):
        self.flags.append(enabled)


class FailingRuntime(FakeRuntime):
    def set_detection_enabled(self, enabled):
        raise RuntimeError("publisher unavailable")


class ParserTests(unittest.TestCase):
    def test_normalizes_filters_and_clamps_boxes(self):
        event = parse_multitask_result(json.dumps(SAMPLE), 0.5, received_at=100,
                                       received_monotonic=50, model="test.hbm")
        self.assertEqual(event["source_frame_sequence"], 42)
        self.assertEqual(event["detection_count"], 2)
        self.assertEqual(event["detections"][0]["label"], "person")
        self.assertEqual(event["detections"][0]["bbox_normalized"]["x1"], 0.010417)
        self.assertEqual(event["detections"][1]["bbox"]["x2"], 960.0)
        self.assertEqual(event["detections"][1]["bbox"]["y2"], 544.0)

    def test_rejects_invalid_payload(self):
        with self.assertRaises(ValueError):
            parse_multitask_result("not-json")
        with self.assertRaises(ValueError):
            parse_multitask_result(json.dumps({"model_input_width": 0, "model_input_height": 1}))


class ProcessorTests(unittest.TestCase):
    def config(self, **overrides):
        config = {
            "enabled": True,
            "auto_start": False,
            "manage_source_enable": True,
            "probe_existing_stream_seconds": 0,
            "source_stale_after_seconds": 1,
            "warmup_seconds": 0.01,
            "confidence_threshold": 0.5,
            "result_topic": "/result",
            "enable_topic": "/flag",
            "model": "test.hbm",
        }
        config.update(overrides)
        return config

    def test_start_enables_owned_source_and_stop_disables_it(self):
        runtime = FakeRuntime()
        processor = ObjectDetectionProcessor(self.config(), ResultBus(), runtime)
        processor.start()
        self.assertEqual(runtime.flags, [True])
        self.assertTrue(processor.status()["owns_source_enable"])
        processor.stop()
        self.assertEqual(runtime.flags, [True, False])

    def test_existing_stream_is_not_disabled_on_stop(self):
        runtime = FakeRuntime()
        processor = ObjectDetectionProcessor(self.config(), ResultBus(), runtime)
        processor.handle_raw_result(json.dumps(SAMPLE), time.time(), time.monotonic())
        processor.start()
        processor.stop()
        self.assertEqual(runtime.flags, [])

    def test_active_processor_publishes_normalized_result(self):
        runtime = FakeRuntime()
        bus = ResultBus()
        processor = ObjectDetectionProcessor(self.config(manage_source_enable=False), bus, runtime)
        processor.start()
        processor.handle_raw_result(json.dumps(SAMPLE), 100, time.monotonic())
        self.assertEqual(bus.latest_result()["detection_count"], 2)
        self.assertEqual(processor.status()["output_frames"], 1)

    def test_stopped_processor_observes_source_without_publishing(self):
        bus = ResultBus()
        processor = ObjectDetectionProcessor(self.config(), bus, FakeRuntime())
        processor.handle_raw_result(json.dumps(SAMPLE), time.time(), time.monotonic())
        self.assertIsNone(bus.latest_result())
        self.assertTrue(processor.status()["source_online"])

    def test_start_failure_is_reported(self):
        processor = ObjectDetectionProcessor(self.config(), ResultBus(), FailingRuntime())
        with self.assertRaises(RuntimeError):
            processor.start()
        self.assertEqual(processor.status()["state"], "failed")
        self.assertEqual(processor.status()["last_error"]["code"], "start_failed")


if __name__ == "__main__":
    unittest.main()
