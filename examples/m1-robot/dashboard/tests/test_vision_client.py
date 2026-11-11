import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vision_client import VisionServiceClient, VisionServiceTransportError


def response(status, payload):
    return status, json.dumps(payload).encode("utf-8")


class VisionClientTests(unittest.TestCase):
    def config(self):
        return {
            "enabled": True,
            "base_url": "http://127.0.0.1:8092",
            "request_timeout_seconds": 1.5,
            "cache_ttl_seconds": 0.8,
            "refresh_interval_seconds": 1.0,
        }

    def test_status_is_cached(self):
        calls = []

        def transport(method, path, payload, timeout):
            calls.append((method, path, payload, timeout))
            return response(200, {"status": "ok", "processors": []})

        client = VisionServiceClient(self.config(), transport)
        self.assertTrue(client.status(refresh=True)["online"])
        self.assertTrue(client.status()["online"])
        self.assertEqual(len(calls), 1)

    def test_transport_failure_degrades_without_raising(self):
        def transport(*_args):
            raise VisionServiceTransportError("connection refused")

        client = VisionServiceClient(self.config(), transport)
        self.assertFalse(client.status(refresh=True)["online"])
        code, latest = client.latest_detection()
        self.assertEqual(code, 503)
        self.assertEqual(latest["error"]["code"], "vision_service_unavailable")

    def test_latest_preserves_stale_response(self):
        def transport(_method, _path, _payload, _timeout):
            return response(200, {"available": False, "stale": True})

        client = VisionServiceClient(self.config(), transport)
        code, data = client.latest_detection()
        self.assertEqual(code, 200)
        self.assertTrue(data["stale"])

    def test_control_uses_fixed_processor_path(self):
        calls = []

        def transport(method, path, payload, timeout):
            calls.append((method, path, payload, timeout))
            if path == "/api/vision/status":
                return response(200, {"status": "ok", "processors": []})
            return response(200, {"ok": True})

        client = VisionServiceClient(self.config(), transport)
        code, data = client.control_detection("start")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(
            calls[0][:2],
            ("POST", "/api/vision/processors/object_detection/start"),
        )
        with self.assertRaises(ValueError):
            client.control_detection("restart")


if __name__ == "__main__":
    unittest.main()
