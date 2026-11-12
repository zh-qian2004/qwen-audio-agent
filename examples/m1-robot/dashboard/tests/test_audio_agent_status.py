import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audio_agent_status import AudioAgentStatus


HEARTBEAT = {
    "instance_id": "m1-robot-primary",
    "label": "M1 Robot",
    "connector": {
        "gateway": {"state": "ready"},
        "audio_input": {"state": "forwarding"},
        "audio_output": {"state": "idle"},
    },
    "gateway": {
        "status": "ready",
        "realtime": {"provider": "dashscope", "model": "qwen-audio-realtime"},
        "backend": {"status": "ready", "model": "qwen3"},
        "voice_clients": {"connected": 1},
    },
}


class AudioAgentStatusTests(unittest.TestCase):
    def test_fresh_ready_heartbeat_is_online(self):
        status = AudioAgentStatus(stale_after_seconds=6)
        result = status.update(HEARTBEAT)

        self.assertTrue(result["online"])
        self.assertEqual(result["state"], "online")
        self.assertEqual(result["deployment_mode"], "cloud")
        card = status.service_card()
        self.assertEqual(card["status"], "online")
        self.assertIn("qwen-audio-realtime", card["detail"])
        self.assertIn("qwen3", card["detail"])
        self.assertIn({"label": "部署位置", "value": "云端"}, card["metrics"])

    def test_explicit_edge_heartbeat(self):
        status = AudioAgentStatus()
        heartbeat = {**HEARTBEAT, "deployment_mode": "edge"}
        result = status.update(heartbeat)
        self.assertEqual(result["deployment_mode"], "edge")
        self.assertIn({"label": "部署位置", "value": "端侧"}, status.service_card()["metrics"])

    def test_unknown_instance_is_not_guessed(self):
        status = AudioAgentStatus()
        result = status.update({**HEARTBEAT, "instance_id": "another-agent"})
        self.assertEqual(result["deployment_mode"], "unknown")

    def test_missing_heartbeat_is_offline(self):
        result = AudioAgentStatus().snapshot()
        self.assertFalse(result["online"])
        self.assertEqual(result["state"], "offline")
        self.assertEqual(result["deployment_mode"], "unknown")

    def test_old_heartbeat_is_stale(self):
        status = AudioAgentStatus(stale_after_seconds=1)
        with patch("audio_agent_status.time.monotonic", side_effect=[10.0, 10.0, 12.5]):
            status.update(HEARTBEAT)
            result = status.snapshot()
        self.assertEqual(result["state"], "stale")
        self.assertFalse(result["online"])


if __name__ == "__main__":
    unittest.main()
