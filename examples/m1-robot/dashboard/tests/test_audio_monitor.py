"""Dashboard 音频代理：HTTP 电平代理、WebSocket PCM 代理、离线降级。"""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audio_monitor import AudioMonitor


LEVEL_PAYLOAD = {
    "active": True,
    "online": True,
    "device": "/audio/capture_pcm",
    "topic": "/audio/capture_pcm",
    "source_label": "厂商 media_audio 处理后麦克风流",
    "sample_rate_hz": 16000,
    "sample_format": "S16_LE",
    "channels": 1,
    "encoding": "pcm_16000_16_1",
    "frame_ms": 20,
    "chunk_bytes": 640,
    "level_db": -24.5,
    "peak_db": -12.0,
    "channel_levels_db": [-24.5],
    "history": [-30.0, -24.5],
    "history_seconds": 0.2,
    "updated_at": time.time(),
    "packet_count": 42,
    "input_rate_hz": 15.6,
    "output_rate_hz": 50.0,
    "silence_ratio": 0.2,
}


def fake_http(payload=None, error=None):
    def _get(url, timeout):
        if error is not None:
            raise error
        return dict(payload or LEVEL_PAYLOAD)

    return _get


class FakeConnection:
    def __init__(self, messages, error_after=False):
        self.messages = list(messages)
        self.error_after = error_after

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def recv(self, timeout=None):
        if self.messages:
            return self.messages.pop(0)
        if self.error_after:
            raise ConnectionError("stream closed")
        raise TimeoutError

    def close(self):
        pass


def start_meta():
    return json.dumps({
        "type": "audio_start",
        "stream_id": "abc123",
        "encoding": "pcm_16000_16_1",
        "sample_rate": 16000,
        "sample_width": 2,
        "channels": 1,
        "frame_ms": 20,
        "frame_bytes": 640,
        "delivery": "continuous",
    })


class AudioProxyHttpTest(unittest.TestCase):
    def test_http_proxy_maps_audio_service_level(self):
        monitor = AudioMonitor({"base_url": "http://127.0.0.1:8091"}, http_get=fake_http())
        self.assertTrue(monitor.refresh())
        state = monitor.snapshot()
        self.assertTrue(state["active"])
        self.assertTrue(state["online"])
        self.assertEqual(state["level_db"], -24.5)
        self.assertEqual(state["peak_db"], -12.0)
        self.assertEqual(state["chunk_bytes"], 640)
        self.assertEqual(state["sample_rate_hz"], 16000)
        self.assertEqual(state["encoding"], "pcm_16000_16_1")
        self.assertEqual(state["history"], [-30.0, -24.5])
        self.assertEqual(state["proxy"]["service_online"], True)
        self.assertIsNone(state["error"])

    def test_http_proxy_reports_structured_offline(self):
        monitor = AudioMonitor(
            {"base_url": "http://127.0.0.1:8091"},
            http_get=fake_http(error=ConnectionRefusedError("refused")),
        )
        self.assertFalse(monitor.refresh())
        state = monitor.snapshot()
        self.assertFalse(state["active"])
        self.assertFalse(state["online"])
        self.assertIsNone(state["level_db"])
        self.assertEqual(state["error_code"], "audio_service_unavailable")
        self.assertIn("ConnectionRefusedError", state["error_detail"])
        self.assertFalse(state["proxy"]["service_online"])

    def test_http_proxy_talks_to_real_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(LEVEL_PAYLOAD).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monitor = AudioMonitor({
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "poll_interval_seconds": 0.05,
            })
            self.assertTrue(monitor.refresh())
            self.assertEqual(monitor.snapshot()["level_db"], -24.5)
            self.assertEqual(monitor.snapshot()["packet_count"], 42)
        finally:
            server.shutdown()
            server.server_close()


class AudioProxyWebSocketTest(unittest.TestCase):
    def test_ws_metadata_and_packet_are_exposed_as_legacy_interface(self):
        monitor = AudioMonitor({}, ws_connect=lambda url, **kwargs: FakeConnection([]))
        monitor._handle_ws_message(start_meta())
        monitor._handle_ws_message(b"\x01\x00" * 320)

        packet = monitor.packet_after(0)
        self.assertEqual(packet["sequence"], 1)
        self.assertEqual(packet["chunk_bytes"], 640)
        self.assertEqual(packet["encoding"], "pcm_16000_16_1")
        self.assertEqual(packet["sample_rate_hz"], 16000)
        self.assertEqual(packet["bits_per_sample"], 16)
        self.assertEqual(packet["channels"], 1)
        self.assertAlmostEqual(packet["frame_duration_ms"], 20.0)
        self.assertIsNone(monitor.packet_after(1))

    def test_ws_stats_are_exposed_in_snapshot(self):
        monitor = AudioMonitor({}, http_get=fake_http())
        monitor.refresh()
        monitor._handle_ws_message(start_meta())
        monitor._handle_ws_message(json.dumps({
            "type": "audio_stats",
            "sent_frames": 100,
            "dropped_frames": 3,
            "queue_depth": 0,
            "last_sequence": 199,
            "lag_ms": 12.5,
        }))
        proxy = monitor.snapshot()["proxy"]
        self.assertEqual(proxy["sent_frames"], 100)
        self.assertEqual(proxy["dropped_frames"], 3)
        self.assertEqual(proxy["lag_ms"], 12.5)
        self.assertEqual(proxy["stream_id"], "abc123")

    def test_ws_reconnects_after_disconnect(self):
        calls = []

        def connect(url, **kwargs):
            calls.append(url)
            return FakeConnection([start_meta()], error_after=True)

        monitor = AudioMonitor(
            {"reconnect_min_seconds": 0.05, "reconnect_max_seconds": 0.1},
            http_get=fake_http(error=ConnectionRefusedError()),
            ws_connect=connect,
        )
        monitor.start()
        try:
            deadline = time.monotonic() + 2.0
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            monitor.stop()
        self.assertGreaterEqual(len(calls), 2)
        self.assertGreaterEqual(monitor.reconnects, 1)
        self.assertIn("/ws/audio/input", calls[0])
        self.assertIn("delivery=continuous", calls[0])

    def test_ws_url_derives_from_http_base(self):
        monitor = AudioMonitor({"base_url": "http://127.0.0.1:8091"})
        self.assertEqual(
            monitor.ws_url,
            "ws://127.0.0.1:8091/ws/audio/input?delivery=continuous&pre_roll_ms=0",
        )


class AudioProxyStreamClientsTest(unittest.TestCase):
    def test_stream_client_count_is_tracked(self):
        monitor = AudioMonitor({}, http_get=fake_http())
        monitor.stream_connected()
        monitor.stream_connected()
        monitor.refresh()
        self.assertEqual(monitor.snapshot()["stream_clients"], 2)
        monitor.stream_disconnected()
        self.assertEqual(monitor.snapshot()["stream_clients"], 1)


if __name__ == "__main__":
    unittest.main()
