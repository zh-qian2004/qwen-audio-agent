"""State routes keep the live and historical payloads separate."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import fcntl  # noqa: F401
    import httpx  # noqa: F401
    import uvicorn  # noqa: F401
    from fastapi.testclient import TestClient

    import main

    AVAILABLE = True
except ImportError as error:  # pragma: no cover - development host
    AVAILABLE = False
    SKIP_REASON = str(error)


class StateRoutesTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_current_state_excludes_history(self):
        with patch.object(main, "_state_payload", return_value={"battery": {"power": 80}}) as payload:
            response = self.client.get("/api/state/current")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"battery": {"power": 80}})
        payload.assert_called_once_with(include_history=False)

    def test_legacy_state_keeps_history(self):
        expected = {"history": {"battery": []}}
        with patch.object(main, "_state_payload", return_value=expected) as payload:
            response = self.client.get("/api/state")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        payload.assert_called_once_with(include_history=True)


if not AVAILABLE:
    StateRoutesTest = unittest.skip(SKIP_REASON)(StateRoutesTest)


if __name__ == "__main__":
    unittest.main()
