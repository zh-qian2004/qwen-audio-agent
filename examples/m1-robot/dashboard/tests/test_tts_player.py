"""Dashboard TTS 客户端测试：全部使用假 transport，不访问真实 audio_service。"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tts_player import (  # noqa: E402
    AudioServiceClient,
    AudioServiceTransportError,
    TTSRequestError,
)


CONFIG = {
    "enabled": True,
    "base_url": "http://127.0.0.1:8091",
    "max_text_length": 20,
    "queue_capacity": 4,
    "status_timeout_seconds": 1,
    "speak_timeout_seconds": 2,
}


def task(task_id="t1", phase="queued", **overrides):
    payload = {
        "task_id": task_id,
        "phase": phase,
        "text": "你好",
        "text_length": 2,
        "queue_position": 0,
        "client_request_id": None,
        "voice": "longanhuan_v3.1",
        "emotion": "neutral",
        "speech_rate": 1.0,
        "created_at": 1000.0,
        "finished_at": None,
        "timestamps": {"queued": 1000.0},
        "durations": {},
        "total_seconds": None,
        "audio": {"bytes": 9000},
        "pcm": {"sample_rate": 48000, "channels": 2, "bytes": 19200},
        "publish": {"packets": 2, "chunk_bytes": 11520, "duration_seconds": 0.12},
        "remaining_playback_seconds": 0.0,
        "error": None,
    }
    payload.update(overrides)
    return payload


def service_status(**overrides):
    payload = {
        "ok": True,
        "version": "1.0.0",
        "uptime_seconds": 12.0,
        "model": "qwen-audio-3.1-tts-flash",
        "voice": "longanhuan_v3.1",
        "provider": {
            "available": True,
            "unavailable_reason": None,
            "model": "qwen-audio-3.1-tts-flash",
            "voice": "longanhuan_v3.1",
            "requests": 3,
            "successes": 3,
            "failures": 0,
            "last_latency_ms": 820.0,
            "last_error": None,
            "api_key_configured": True,
            "api_key_masked": "sk-abc****",
        },
        "current_task": None,
        "queue": {"capacity": 8, "pending": 1, "items": []},
        "queue_size": 1,
        "queue_capacity": 8,
        "controls": {
            "voices": [
                {"id": "longanhuan_v3.1", "label": "龙安欢"},
                {"id": "xunanchuan_v3.1", "label": "许南川"},
            ],
            "emotions": [
                {"id": "neutral", "label": "自然"},
                {"id": "excited", "label": "兴奋"},
            ],
            "defaults": {"voice": "longanhuan_v3.1", "emotion": "neutral", "speech_rate": 1.0},
            "speech_rate": {"min": 0.5, "max": 2.0, "step": 0.05, "default": 1.0},
        },
        "recent_tasks": [task("t1", "succeeded", total_seconds=2.5)],
        "ros": {
            "available": True,
            "error": None,
            "topic": "/audio/speaker_pcm",
            "encoding": "pcm_48000_16_2",
            "subscribers": 1,
            "published_packets": 4,
            "capture": {"rate_hz": 15.6, "active": True},
        },
        "media_audio": {"process": "media_audio", "running": True, "pid": 21938, "pids": [21938]},
        "last_error": None,
        "playback_control": {"enabled": False},
        "stats": {"total": 1, "succeeded": 1},
    }
    payload.update(overrides)
    return payload


def audio_health():
    return {
        "status": "ok", "version": "1.1.0", "uptime_seconds": 120.0,
        "last_error": None,
    }


def audio_gateway_status():
    return {
        "online": True,
        "capture": {
            "online": True, "topic": "/audio/capture_pcm", "encoding": "pcm_16000_16_1",
            "sample_rate": 16000, "channels": 1, "input_rate_hz": 15.6,
            "last_frame_age_ms": 12.0, "invalid_packets": 0,
        },
        "rechunker": {"output_rate_hz": 50.0, "output_frames": 500},
        "ring_buffer": {"available_ms": 5000},
        "consumers": [{
            "name": "ws-input-test", "policy": "drop_oldest", "capacity": 50,
            "queue_depth": 1, "dropped_frames": 0, "lag_ms": 20.0,
        }],
    }


def audio_sessions():
    return {"items": [], "available_types": ["mock_asr", "mock_tse"]}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.routes = {}

    def on(self, method, path, status=200, payload=None, error=None):
        self.routes[(method, path.split("?")[0])] = (status, payload, error)

    def __call__(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload, timeout))
        key = (method, path.split("?")[0])
        if key not in self.routes:
            raise AudioServiceTransportError(f"no route for {method} {path}")
        status, body, error = self.routes[key]
        if error is not None:
            raise error
        return status, json.dumps(body if body is not None else {}).encode("utf-8")

    def last(self):
        return self.calls[-1]


class ClientStatusTests(unittest.TestCase):
    def test_online_status_aggregates_backend(self):
        transport = FakeTransport()
        transport.on("GET", "/api/tts/status", payload=service_status())
        client = AudioServiceClient(CONFIG, transport=transport)

        status = client.status(refresh=True)

        self.assertTrue(status["online"])
        self.assertEqual(status["service"]["version"], "1.0.0")
        self.assertEqual(status["engine"]["model"], "qwen-audio-3.1-tts-flash")
        self.assertEqual(status["engine"]["voice"], "longanhuan_v3.1")
        self.assertEqual(status["engine"]["topic"], "/audio/speaker_pcm")
        self.assertEqual(status["queue"]["pending"], 1)
        self.assertEqual(status["ros"]["capture_rate_hz"], 15.6)
        self.assertEqual(status["media_audio"]["pid"], 21938)
        self.assertEqual(status["history"][0]["job_id"], "t1")
        self.assertEqual(status["history"][0]["elapsed_ms"], 2500.0)
        self.assertEqual(status["controls"]["voices"][1]["id"], "xunanchuan_v3.1")
        self.assertFalse(status["arbitration"]["enabled"])

    def test_offline_status_marks_service_unavailable(self):
        transport = FakeTransport()
        transport.on("GET", "/api/tts/status", error=AudioServiceTransportError("connection refused"))
        client = AudioServiceClient(CONFIG, transport=transport)

        status = client.status(refresh=True)

        self.assertFalse(status["online"])
        self.assertFalse(status["service"]["online"])
        self.assertEqual(status["last_error"]["code"], "service_unavailable")
        self.assertFalse(status["engine"]["available"])

    def test_cached_status_does_not_call_transport(self):
        transport = FakeTransport()
        transport.on("GET", "/api/tts/status", payload=service_status())
        client = AudioServiceClient(CONFIG, transport=transport)
        client.status(refresh=True)
        before = len(transport.calls)

        for _ in range(5):
            client.cached_status()

        self.assertEqual(len(transport.calls), before)

    def test_status_never_leaks_api_key(self):
        transport = FakeTransport()
        transport.on("GET", "/api/tts/status", payload=service_status())
        client = AudioServiceClient(CONFIG, transport=transport)

        payload = json.dumps(client.status(refresh=True), ensure_ascii=False)

        self.assertNotIn("sk-", payload)
        self.assertNotIn("api_key_masked", payload)


class AudioServicesOverviewTests(unittest.TestCase):
    def configure_routes(self, transport):
        transport.on("GET", "/api/health", payload=audio_health())
        transport.on("GET", "/api/audio/status", payload=audio_gateway_status())
        transport.on("GET", "/api/audio/sessions", payload=audio_sessions())
        transport.on("GET", "/api/tts/status", payload=service_status())

    def test_audio_services_aggregates_current_components(self):
        transport = FakeTransport()
        self.configure_routes(transport)
        client = AudioServiceClient(CONFIG, transport=transport)

        result = client.audio_services()

        self.assertTrue(result["online"])
        self.assertEqual(result["service"]["version"], "1.1.0")
        self.assertTrue(result["summary"]["capture_online"])
        self.assertEqual(result["summary"]["consumer_count"], 1)
        services = {item["id"]: item for item in result["services"]}
        self.assertEqual(services["capture"]["status"], "online")
        self.assertEqual(services["tts"]["status"], "online")
        self.assertEqual(services["playback"]["status"], "online")
        self.assertEqual(services["asr"]["status"], "mock")
        self.assertEqual(services["tse"]["status"], "mock")
        self.assertEqual(result["connections"][0]["name"], "ws-input-test")

    def test_audio_services_returns_structured_offline_payload(self):
        transport = FakeTransport()
        transport.on("GET", "/api/health", error=AudioServiceTransportError("connection refused"))
        client = AudioServiceClient(CONFIG, transport=transport)

        result = client.audio_services()

        self.assertFalse(result["online"])
        self.assertEqual(result["service"]["status"], "offline")
        self.assertEqual(len(result["services"]), 6)
        self.assertEqual(result["services"][0]["status"], "offline")

    def test_audio_services_never_exposes_provider_key_metadata(self):
        transport = FakeTransport()
        self.configure_routes(transport)
        client = AudioServiceClient(CONFIG, transport=transport)

        serialized = json.dumps(client.audio_services(), ensure_ascii=False)

        self.assertNotIn("sk-", serialized)
        self.assertNotIn("api_key_masked", serialized)


class ClientSpeakTests(unittest.TestCase):
    def test_speak_forwards_to_audio_service(self):
        transport = FakeTransport()
        transport.on("POST", "/api/tts/speak", status=202, payload={
            "ok": True,
            "deduplicated": False,
            "task_id": "abc123",
            "phase": "queued",
            "queue_position": 2,
            "queue_size": 3,
            "model": "qwen-audio-3.1-tts-flash",
            "voice": "longanhuan_v3.1",
            "task": task("abc123", "queued", queue_position=2),
        })
        client = AudioServiceClient(CONFIG, transport=transport)

        result = client.speak(
            "  你好  ",
            client_request_id="req-1",
            voice="xunanchuan_v3.1",
            emotion="excited",
            speech_rate=1.25,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["task_id"], "abc123")
        self.assertEqual(result["ahead_in_queue"], 2)
        method, path, payload, timeout = transport.last()
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/tts/speak")
        self.assertEqual(payload["text"], "你好")
        self.assertEqual(payload["client_request_id"], "req-1")
        self.assertEqual(payload["priority"], "normal")
        self.assertFalse(payload["interrupt"])
        self.assertEqual(payload["voice"], "xunanchuan_v3.1")
        self.assertEqual(payload["emotion"], "excited")
        self.assertEqual(payload["speech_rate"], 1.25)
        self.assertEqual(timeout, CONFIG["speak_timeout_seconds"])

    def test_speak_marks_idempotent_replay(self):
        transport = FakeTransport()
        transport.on("POST", "/api/tts/speak", status=200, payload={
            "ok": True, "deduplicated": True, "task_id": "abc123",
            "phase": "publishing", "queue_position": None, "task": task("abc123", "publishing"),
        })
        client = AudioServiceClient(CONFIG, transport=transport)
        result = client.speak("你好", client_request_id="req-1")
        self.assertTrue(result["idempotent_replay"])

    def test_speak_offline_returns_503(self):
        transport = FakeTransport()
        transport.on("POST", "/api/tts/speak", error=AudioServiceTransportError("timed out"))
        client = AudioServiceClient(CONFIG, transport=transport)

        with self.assertRaises(TTSRequestError) as info:
            client.speak("你好")

        self.assertEqual(info.exception.code, "service_unavailable")
        self.assertEqual(info.exception.status_code, 503)
        self.assertIn("audio_service", info.exception.message)

    def test_speak_maps_backend_errors(self):
        transport = FakeTransport()
        transport.on("POST", "/api/tts/speak", status=429, payload={
            "error": {"code": "queue_full", "message": "playback queue is full (capacity=8)", "detail": {"capacity": 8}}
        })
        client = AudioServiceClient(CONFIG, transport=transport)

        with self.assertRaises(TTSRequestError) as info:
            client.speak("你好")

        self.assertEqual(info.exception.code, "queue_full")
        self.assertEqual(info.exception.status_code, 429)
        self.assertEqual(info.exception.detail["capacity"], 8)

    def test_speak_validates_locally(self):
        transport = FakeTransport()
        client = AudioServiceClient(CONFIG, transport=transport)

        with self.assertRaises(TTSRequestError) as empty:
            client.speak("   ")
        self.assertEqual(empty.exception.code, "empty_text")

        with self.assertRaises(TTSRequestError) as too_long:
            client.speak("一" * 30)
        self.assertEqual(too_long.exception.code, "text_too_long")

        with self.assertRaises(TTSRequestError) as wrong_type:
            client.speak(None)
        self.assertEqual(wrong_type.exception.code, "invalid_text")

        self.assertEqual(transport.calls, [])

    def test_disabled_client_rejects_speak(self):
        client = AudioServiceClient({**CONFIG, "enabled": False}, transport=FakeTransport())
        with self.assertRaises(TTSRequestError) as info:
            client.speak("你好")
        self.assertEqual(info.exception.status_code, 503)


class ClientStopAndHistoryTests(unittest.TestCase):
    def test_stop_playback_proxies_result(self):
        transport = FakeTransport()
        transport.on("POST", "/api/tts/stop", payload={
            "ok": True,
            "cancelled_queued": 2,
            "current_task_id": "t1",
            "estimated_remaining_playback_seconds": 0.12,
        })
        client = AudioServiceClient(CONFIG, transport=transport)

        result = client.stop_playback()

        self.assertTrue(result["ok"])
        self.assertEqual(result["cancelled_queued"], 2)
        self.assertEqual(result["estimated_remaining_playback_seconds"], 0.12)

    def test_history_reads_backend(self):
        transport = FakeTransport()
        transport.on("GET", "/api/tts/history", payload={"items": [task("t9", "succeeded")]})
        client = AudioServiceClient(CONFIG, transport=transport)

        items = client.history(5)

        self.assertEqual(items[0]["job_id"], "t9")

    def test_history_falls_back_to_local_when_offline(self):
        transport = FakeTransport()
        transport.on("POST", "/api/tts/speak", status=202, payload={
            "ok": True, "deduplicated": False, "task_id": "local-1",
            "phase": "queued", "queue_position": 0, "task": task("local-1"),
        })
        transport.on("GET", "/api/tts/history", error=AudioServiceTransportError("down"))
        client = AudioServiceClient(CONFIG, transport=transport)
        client.speak("你好")

        items = client.history(5)

        self.assertEqual(items[0]["job_id"], "local-1")


class StaticVoiceFallbackTests(unittest.TestCase):
    """离线时下拉框保留 index.html 内置选项，需与 audio_service 音色清单一致。"""

    VOICES = [
        "longanhuan_v3.1",
        "longanlingxin_v3.1",
        "longanfengyue_v3.1",
        "xunanchuan_v3.1",
        "yuxiaoyun_v3.1",
        "qiaoxiaojiao_v3.1",
        "anmingyuan_v3.1",
        "wenhuaiqing_v3.1",
        "baianran_v3.1",
        "xuyanchu_v3.1",
    ]

    def test_offline_fallback_lists_ten_voices_in_order(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        block = html.split('id="tts-voice-select"', 1)[1].split("</select>", 1)[0]
        self.assertEqual(re.findall(r'<option value="([^"]+)"', block), self.VOICES)

    def test_audio_service_view_is_wired_into_navigation(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="audio-services"', html)
        self.assertIn('id="audio-services"', html)
        self.assertIn('id="audio-services-grid"', html)


if __name__ == "__main__":
    unittest.main()
