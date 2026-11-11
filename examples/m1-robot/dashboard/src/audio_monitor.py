"""Dashboard 音频代理。

Dashboard 不再订阅 ROS ``/audio/capture_pcm``，也不再自行解析 PCM：

* 电平：轮询 ``audio_service`` 的 ``GET /api/audio/level``（默认 10 Hz）
* PCM：通过 WebSocket 连接 ``audio_service /ws/audio/input``（20 ms / 640 字节帧）

对外仍保留原有 ``snapshot()`` / ``packet_after()`` 接口，因此
``GET /api/audio/level`` 与 ``WS /ws/audio/pcm`` 的调用方（Agent、前端）无需改动。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("dashboard.audio")


class AudioMonitor:
    def __init__(self, config, http_get=None, ws_connect=None, on_activity=None):
        config = config or {}
        self.base_url = str(config.get("base_url", "http://127.0.0.1:8091")).rstrip("/")
        self.level_path = str(config.get("level_path", "/api/audio/level"))
        self.ws_path = str(config.get("ws_path", "/ws/audio/input?delivery=continuous&pre_roll_ms=0"))
        self.topic = str(config.get("topic", "/audio/capture_pcm"))
        self.source_label = str(config.get("source_label", "厂商 media_audio 处理后麦克风流"))
        self.fallback_rate = int(config.get("sample_rate_hz", 16000))
        self.fallback_format = str(config.get("sample_format", "S16_LE"))
        self.fallback_channels = int(config.get("channels", 1))
        self.poll_interval = max(0.02, float(config.get("poll_interval_seconds", 0.1)))
        self.request_timeout = max(0.1, float(config.get("request_timeout_seconds", 1.0)))
        self.stale_after_seconds = float(config.get("stale_after_seconds", 2.0))
        self.reconnect_min = max(0.1, float(config.get("reconnect_min_seconds", 0.5)))
        self.reconnect_max = max(self.reconnect_min, float(config.get("reconnect_max_seconds", 5.0)))

        self.http_get = http_get or self._default_http_get
        self.ws_connect = ws_connect or self._default_ws_connect
        self.on_activity = on_activity

        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self._threads = []
        self._started = False

        self.service_online = False
        self.level_error = None
        self.level_payload = None
        self.level_at_mono = None

        self.ws_connected = False
        self.ws_error = None
        self.ws_meta = None
        self.ws_stats = {}
        self.reconnects = 0
        self.latest_packet = None
        self.latest_packet_meta = None
        self.latest_sequence = 0
        self.latest_packet_at = None
        self.stream_clients = 0

    # ---- 生命周期 -------------------------------------------------
    def start(self):
        if self._started:
            return
        self._started = True
        self.stop_event.clear()
        for target, name in ((self._poll_loop, "audio-level-proxy"), (self._ws_loop, "audio-pcm-proxy")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        LOG.info("audio proxy started base_url=%s", self.base_url)

    def stop(self):
        self._started = False
        self.stop_event.set()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads = []

    def touch(self):
        """兼容旧接口：电平由后台线程按固定频率刷新，无需请求时再拉取。"""

    # ---- HTTP 电平代理 --------------------------------------------
    def _default_http_get(self, url, timeout):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _poll_loop(self):
        while not self.stop_event.is_set():
            self.refresh()
            self.stop_event.wait(self.poll_interval)

    def refresh(self):
        """拉取一次电平；返回 audio_service 是否可用。单测可直接调用。"""
        url = f"{self.base_url}{self.level_path}"
        try:
            payload = self.http_get(url, self.request_timeout)
            if not isinstance(payload, dict):
                raise ValueError("音频服务返回了非 JSON 对象")
        except urllib.error.HTTPError as exc:
            self._mark_offline(f"HTTP {exc.code}")
            return False
        except Exception as exc:
            self._mark_offline(f"{type(exc).__name__}: {exc}")
            return False
        with self.lock:
            self.level_payload = payload
            self.level_at_mono = time.monotonic()
            self.service_online = True
            self.level_error = None
        if self.on_activity is not None:
            try:
                self.on_activity(payload)
            except Exception:  # pragma: no cover - 回调异常不影响代理
                LOG.debug("audio on_activity 回调异常", exc_info=True)
        return True

    def _mark_offline(self, message):
        with self.lock:
            if self.service_online:
                LOG.warning("audio_service 电平代理离线：%s", message)
            self.service_online = False
            self.level_error = message

    # ---- WebSocket PCM 代理 ---------------------------------------
    def _default_ws_connect(self, url, **kwargs):
        from websockets.sync.client import connect

        return connect(url, **kwargs)

    def _ws_loop(self):
        backoff = self.reconnect_min
        while not self.stop_event.is_set():
            try:
                with self.ws_connect(self.ws_url, open_timeout=2, close_timeout=1) as connection:
                    self._set_ws_state(True, None)
                    backoff = self.reconnect_min
                    self._consume_ws(connection)
            except Exception as exc:
                exhausted = self.stop_event.is_set()
                self._set_ws_state(False, f"{type(exc).__name__}: {exc}")
                if exhausted:
                    return
                self.reconnects += 1
                self.stop_event.wait(backoff)
                backoff = min(self.reconnect_max, backoff * 2)

    @property
    def ws_url(self):
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        else:
            base = "ws://" + base[len("http://"):]
        return f"{base}{self.ws_path}"

    def _consume_ws(self, connection):
        while not self.stop_event.is_set():
            try:
                message = connection.recv(timeout=1.0)
            except TimeoutError:
                continue
            self._handle_ws_message(message)

    def _handle_ws_message(self, message):
        if isinstance(message, (bytes, bytearray, memoryview)):
            data = bytes(message)
            with self.lock:
                meta = dict(self.ws_meta or {})
                self.latest_sequence += 1
                self.latest_packet = data
                self.latest_packet_at = time.monotonic()
                self.latest_packet_meta = meta
            return

        try:
            payload = json.loads(message)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "audio_start":
            with self.lock:
                self.ws_meta = payload
            LOG.info(
                "audio_service PCM 流已连接 stream=%s %s Hz/%dch frame=%sms",
                payload.get("stream_id"),
                payload.get("sample_rate"),
                int(payload.get("channels") or 1),
                payload.get("frame_ms"),
            )
        elif kind == "audio_stats":
            with self.lock:
                self.ws_stats = payload

    def _set_ws_state(self, connected, error):
        with self.lock:
            if connected != self.ws_connected:
                LOG.info("audio_service PCM 流 %s", "connected" if connected else "disconnected")
            self.ws_connected = connected
            self.ws_error = error

    # ---- 对外快照 -------------------------------------------------
    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            payload = dict(self.level_payload) if self.level_payload else {}
            online = self.service_online and self.level_at_mono is not None
            fresh = online and (now - self.level_at_mono) <= self.stale_after_seconds
            error = self.level_error
            ws_connected = self.ws_connected
            ws_error = self.ws_error
            ws_meta = dict(self.ws_meta or {})
            ws_stats = dict(self.ws_stats or {})
            stream_clients = self.stream_clients
            sequence = self.latest_sequence
            packet_at = self.latest_packet_at
            reconnects = self.reconnects

        if not online or not payload:
            return self._offline_snapshot(now, error, ws_error, stream_clients)

        active = bool(payload.get("active")) and fresh
        stream_error = None if active else (
            "音频服务数据超时" if online and not fresh else payload.get("error")
        )
        chunk_bytes = payload.get("chunk_bytes") or (ws_meta.get("frame_bytes") or 640)
        return {
            "active": active,
            "online": online,
            "device": payload.get("device", self.topic),
            "topic": payload.get("topic", self.topic),
            "source_label": payload.get("source_label", self.source_label),
            "sample_rate_hz": payload.get("sample_rate_hz", self.fallback_rate),
            "sample_format": payload.get("sample_format", self.fallback_format),
            "channels": payload.get("channels", self.fallback_channels),
            "level_db": payload.get("level_db") if active else None,
            "channel_levels_db": payload.get("channel_levels_db") if active else None,
            "peak_db": payload.get("peak_db") if active else None,
            "last_sample_at": payload.get("updated_at"),
            "history": payload.get("history") or [],
            "history_seconds": payload.get("history_seconds", 0.0),
            "encoding": payload.get("encoding", "pcm_16000_16_1"),
            "frame_id": ws_meta.get("stream_id"),
            "chunk_bytes": chunk_bytes,
            "packet_count": payload.get("packet_count", 0),
            "stream_clients": stream_clients,
            "error": stream_error,
            "error_code": None,
            "input_rate_hz": payload.get("input_rate_hz"),
            "output_rate_hz": payload.get("output_rate_hz"),
            "last_frame_age_ms": payload.get("last_frame_age_ms"),
            "silence_ratio": payload.get("silence_ratio"),
            "proxy": {
                "service_online": online,
                "level_age_ms": round((now - self.level_at_mono) * 1000.0, 1)
                if self.level_at_mono
                else None,
                "ws_connected": ws_connected,
                "ws_error": ws_error,
                "stream_id": ws_meta.get("stream_id"),
                "reconnects": reconnects,
                "sent_frames": ws_stats.get("sent_frames"),
                "dropped_frames": ws_stats.get("dropped_frames"),
                "queue_depth": ws_stats.get("queue_depth"),
                "last_sequence": sequence,
                "lag_ms": ws_stats.get("lag_ms"),
                "last_packet_age_ms": round((now - packet_at) * 1000.0, 1) if packet_at else None,
            },
        }

    def _offline_snapshot(self, now, error, ws_error, stream_clients):
        return {
            "active": False,
            "online": False,
            "device": self.topic,
            "topic": self.topic,
            "source_label": self.source_label,
            "sample_rate_hz": self.fallback_rate,
            "sample_format": self.fallback_format,
            "channels": self.fallback_channels,
            "level_db": None,
            "channel_levels_db": [None] * self.fallback_channels,
            "peak_db": None,
            "last_sample_at": None,
            "history": [],
            "history_seconds": 0.0,
            "encoding": "pcm_16000_16_1",
            "frame_id": None,
            "chunk_bytes": None,
            "packet_count": 0,
            "stream_clients": stream_clients,
            "error": "audio_service 离线" if error else None,
            "error_code": "audio_service_unavailable" if error else None,
            "error_detail": error,
            "input_rate_hz": None,
            "output_rate_hz": None,
            "last_frame_age_ms": None,
            "silence_ratio": None,
            "proxy": {
                "service_online": False,
                "level_age_ms": None,
                "ws_connected": False,
                "ws_error": ws_error or error,
                "stream_id": None,
                "reconnects": self.reconnects,
                "sent_frames": None,
                "dropped_frames": None,
                "queue_depth": None,
                "last_sequence": self.latest_sequence,
                "lag_ms": None,
                "last_packet_age_ms": None,
            },
        }

    def stream_connected(self):
        with self.lock:
            self.stream_clients += 1

    def stream_disconnected(self):
        with self.lock:
            self.stream_clients = max(0, self.stream_clients - 1)

    def packet_after(self, sequence):
        """只返回最新 20 ms / 640 字节帧，慢客户端不会产生背压。"""
        with self.lock:
            if self.latest_packet is None or self.latest_sequence <= sequence:
                return None
            meta = dict(self.latest_packet_meta or self.ws_meta or {})
            data = self.latest_packet
            rate = int(meta.get("sample_rate") or self.fallback_rate)
            channels = int(meta.get("channels") or self.fallback_channels)
            sample_width = int(meta.get("sample_width") or 2)
            return {
                "sequence": self.latest_sequence,
                "data": data,
                "encoding": meta.get("encoding") or "pcm_16000_16_1",
                "sample_rate_hz": rate,
                "bits_per_sample": sample_width * 8,
                "channels": channels,
                "chunk_bytes": len(data),
                "frame_duration_ms": round(
                    len(data) / (sample_width * channels) / rate * 1000, 3
                )
                if sample_width and channels and rate
                else None,
                "frame_id": meta.get("stream_id"),
            }
