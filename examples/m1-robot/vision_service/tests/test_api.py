import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from fastapi.testclient import TestClient
    import main
except Exception as error:  # pragma: no cover - dependency gate
    TestClient = None
    IMPORT_ERROR = error
else:
    IMPORT_ERROR = None


class FakeBus:
    def __init__(self):
        self.clients = 0
        self.event = {
            "type": "detection_frame",
            "bus_sequence": 1,
            "received_monotonic": 1.0,
            "detection_count": 0,
            "detections": [],
        }

    def latest_result(self):
        return dict(self.event)

    def recent(self, limit):
        return [dict(self.event)]

    def status(self):
        return {"latest_age_seconds": 0.1, "clients": self.clients}

    def wait_for_update(self, after_sequence, timeout):
        return dict(self.event) if after_sequence < 1 else None

    def client_opened(self):
        self.clients += 1

    def client_closed(self):
        self.clients -= 1


class FakeService:
    def __init__(self):
        self.bus = FakeBus()
        self.started = False
        self.processor_state = "stopped"

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def health(self):
        return {"status": "ok", "processors": self.processor_statuses()}

    def processor_statuses(self):
        return [{"id": "object_detection", "state": self.processor_state}]

    def start_processor(self, processor_id):
        if processor_id != "object_detection":
            raise KeyError(processor_id)
        self.processor_state = "running"
        return self.processor_statuses()[0]

    def stop_processor(self, processor_id):
        if processor_id != "object_detection":
            raise KeyError(processor_id)
        self.processor_state = "stopped"
        return self.processor_statuses()[0]


@unittest.skipIf(TestClient is None, f"FastAPI test dependencies unavailable: {IMPORT_ERROR}")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.original = main.SERVICE
        self.service = FakeService()
        main.SERVICE = self.service
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        main.SERVICE = self.original

    def test_health_and_processor_lifecycle(self):
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")
        started = self.client.post(
            "/api/vision/processors/object_detection/start"
        ).json()
        self.assertTrue(started["ok"])
        self.assertEqual(started["processor"]["state"], "running")
        stopped = self.client.post(
            "/api/vision/processors/object_detection/stop"
        ).json()
        self.assertEqual(stopped["processor"]["state"], "stopped")
        self.assertEqual(
            self.client.post("/api/vision/processors/unknown/start").status_code, 404
        )

    def test_latest_and_history(self):
        latest = self.client.get("/api/vision/detections/latest").json()
        self.assertTrue(latest["available"])
        self.assertFalse(latest["stale"])
        history = self.client.get("/api/vision/detections/history?limit=1").json()
        self.assertEqual(len(history["items"]), 1)
        tracks = self.client.get("/api/vision/tracks/latest").json()
        self.assertTrue(tracks["available"])
        track_history = self.client.get("/api/vision/tracks/history?limit=1").json()
        self.assertEqual(len(track_history["items"]), 1)

    def test_websocket_starts_with_metadata_then_detection(self):
        with self.client.websocket_connect("/ws/vision/events") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "vision_start")
            self.assertEqual(websocket.receive_json()["type"], "detection_frame")


if __name__ == "__main__":
    unittest.main()
