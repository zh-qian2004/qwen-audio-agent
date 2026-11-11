"""Dashboard 音频路由：audio_service 离线时返回结构化 503 / WebSocket 关闭原因。

这些用例需要 Linux + 完整 Dashboard 依赖（fcntl / uvicorn / httpx），
在开发机上会自动跳过，实机部署后执行。
"""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import fcntl  # noqa: F401
    import httpx  # noqa: F401
    import uvicorn  # noqa: F401
    from fastapi.testclient import TestClient

    import main
    from audio_monitor import AudioMonitor

    AVAILABLE = True
except ImportError as error:  # pragma: no cover - 开发机环境
    AVAILABLE = False
    SKIP_REASON = str(error)


class OfflineDegradationTest(unittest.TestCase):
    def setUp(self):
        self.original = main.AUDIO
        self.offline = AudioMonitor(
            {},
            http_get=lambda _url, _timeout: (_ for _ in ()).throw(
                ConnectionRefusedError("connection refused")
            ),
        )
        self.offline.refresh()
        main.AUDIO = self.offline
        self.client = TestClient(main.app)

    def tearDown(self):
        main.AUDIO = self.original

    def test_audio_level_returns_structured_503(self):
        response = self.client.get("/api/audio/level")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["active"])
        self.assertEqual(body["error_code"], "audio_service_unavailable")
        self.assertIn("ConnectionRefusedError", body["error_detail"])

    def test_pcm_websocket_reports_unavailable_and_closes(self):
        with self.client.websocket_connect("/ws/audio/pcm") as websocket:
            message = websocket.receive_json()
        self.assertEqual(message["type"], "audio_unavailable")
        self.assertEqual(message["code"], "audio_service_unavailable")

    def test_health_exposes_audio_backend_state(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(body["audio"]["backend"], "audio_service")
        self.assertFalse(body["audio"]["service_online"])


if not AVAILABLE:
    OfflineDegradationTest = unittest.skip(SKIP_REASON)(OfflineDegradationTest)


if __name__ == "__main__":
    unittest.main()
