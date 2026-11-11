"""Dashboard 的 TTS 客户端。

真正的声音由独立进程 audio_service（127.0.0.1:8091）合成并通过 ROS
/audio/speaker_pcm 交给厂商 media_audio 播放。

本模块不再：
  * 调用 espeak / aplay；
  * 打开或释放 ALSA 设备；
  * 停止 / 重启 media_audio。
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque

LOG = logging.getLogger("dashboard.tts")

PHASES = (
    "queued",
    "synthesizing",
    "decoding",
    "publishing",
    "waiting_playback",
    "succeeded",
    "failed",
    "cancelled",
)
ACTIVE_PHASES = ("queued", "synthesizing", "decoding", "publishing", "waiting_playback")


class TTSRequestError(Exception):
    def __init__(self, code, message, status_code=400, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail

    def as_dict(self):
        payload = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class AudioServiceTransportError(Exception):
    """audio_service 不可达 / 超时。"""


def _detail_text(detail, limit=240):
    if not detail:
        return None
    if isinstance(detail, str):
        return detail[:limit]
    if isinstance(detail, dict):
        parts = [f"{key}={value}" for key, value in detail.items() if value not in (None, "", [], {})]
        return "，".join(parts)[:limit] or None
    return str(detail)[:limit]


class AudioServiceClient:
    """audio_service 的本地 HTTP 客户端（只做转发与状态聚合）。"""

    def __init__(self, config, transport=None):
        self.enabled = bool(config.get("enabled", True))
        self.base_url = str(config.get("base_url", "http://127.0.0.1:8091")).rstrip("/")
        self.status_timeout = float(config.get("status_timeout_seconds", 2))
        self.speak_timeout = float(config.get("speak_timeout_seconds", 8))
        self.cache_ttl = float(config.get("cache_ttl_seconds", 0.8))
        self.refresh_interval = float(config.get("refresh_interval_seconds", 1.0))
        self.max_text_length = int(config.get("max_text_length", 300))
        self.queue_capacity = int(config.get("queue_capacity", 8))
        self.topic = str(config.get("speaker_topic", "/audio/speaker_pcm"))
        self.transport = transport or self._http_request

        self._lock = threading.RLock()
        self._cache = None
        self._cache_at = 0.0
        self._online = False
        self._last_ok_at = None
        self._last_error = None
        self._local_history = deque(maxlen=10)
        self._stop_event = threading.Event()
        self._refresh_thread = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._refresh_thread and self._refresh_thread.is_alive():
            return
        self._stop_event.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, name="tts-status-refresh", daemon=True)
        self._refresh_thread.start()
        LOG.info("audio_service client started, base_url=%s", self.base_url)

    def stop(self):
        self._stop_event.set()
        thread = self._refresh_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self._refresh_thread = None

    def _refresh_loop(self):
        while not self._stop_event.is_set():
            try:
                self.status(refresh=True)
            except Exception:  # pragma: no cover - 后台刷新不应中断
                LOG.exception("audio_service 状态刷新失败")
            self._stop_event.wait(self.refresh_interval)

    # -- HTTP --------------------------------------------------------------
    def _http_request(self, method, path, payload=None, timeout=None):
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.status_timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, OSError) as error:
            raise AudioServiceTransportError(str(getattr(error, "reason", error)))

    def _call(self, method, path, payload=None, timeout=None):
        raw = self.transport(method, path, payload, timeout or self.status_timeout)
        status, body = raw
        data = {}
        if body:
            try:
                data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
            except (ValueError, UnicodeDecodeError):
                data = {}
        return status, data

    # -- public API --------------------------------------------------------
    def speak(self, text, speed=None, client_request_id=None, reason=None,
              actor=None, priority="normal", interrupt=False, voice=None,
              emotion="neutral", speech_rate=1.0):
        if not self.enabled:
            raise TTSRequestError("disabled", "文字播报功能未启用", 503)
        normalized = self._validate_text(text)
        payload = {
            "text": normalized,
            "client_request_id": (client_request_id or "").strip() or None,
            "priority": priority if priority in ("normal", "high") else "normal",
            "interrupt": bool(interrupt),
            "voice": (voice or "").strip() or None,
            "emotion": (emotion or "neutral").strip(),
            "speech_rate": speech_rate,
        }
        try:
            status, data = self._call("POST", "/api/tts/speak", payload, self.speak_timeout)
        except AudioServiceTransportError as error:
            self._mark_offline(str(error))
            raise TTSRequestError(
                "service_unavailable",
                f"audio_service 未运行或不可达（{self.base_url}）",
                503,
                {"url": self.base_url, "transport": str(error)},
            )
        if status not in (200, 202):
            error = (data or {}).get("error") or {}
            raise TTSRequestError(
                error.get("code") or "speak_failed",
                error.get("message") or f"audio_service 返回 HTTP {status}",
                status if 400 <= status < 600 else 502,
                error.get("detail"),
            )

        job = self._normalize_task(data.get("task") or {})
        with self._lock:
            self._online = True
            self._last_ok_at = time.time()
            if job:
                self._local_history.appendleft(job)
        LOG.info("TTS 已转发到 audio_service task_id=%s dedup=%s pos=%s",
                 data.get("task_id"), data.get("deduplicated"), data.get("queue_position"))
        return {
            "accepted": True,
            "idempotent_replay": bool(data.get("deduplicated")),
            "ahead_in_queue": int(data.get("queue_position") or 0),
            "task_id": data.get("task_id"),
            "model": data.get("model"),
            "voice": data.get("voice"),
            "emotion": data.get("emotion") or emotion,
            "speech_rate": data.get("speech_rate") or speech_rate,
            "legacy_speed_ignored": speed is not None,
            "reason": reason,
            "actor": actor,
            "job": job,
        }

    def stop_playback(self):
        try:
            status, data = self._call("POST", "/api/tts/stop", {}, self.speak_timeout)
        except AudioServiceTransportError as error:
            self._mark_offline(str(error))
            raise TTSRequestError(
                "service_unavailable",
                f"audio_service 未运行或不可达（{self.base_url}）",
                503,
                {"url": self.base_url, "transport": str(error)},
            )
        if status not in (200, 202):
            error = (data or {}).get("error") or {}
            raise TTSRequestError(
                error.get("code") or "stop_failed",
                error.get("message") or f"audio_service 返回 HTTP {status}",
                status if 400 <= status < 600 else 502,
                error.get("detail"),
            )
        return {"ok": True, **data}

    def history(self, limit=10):
        try:
            status, data = self._call("GET", f"/api/tts/history?limit={int(limit)}")
            if status == 200:
                return [self._normalize_task(item) for item in data.get("items", [])]
        except AudioServiceTransportError:
            pass
        with self._lock:
            return list(self._local_history)[:limit]

    def audio_services(self):
        """聚合 audio_service 的输入、处理会话、TTS 与播放状态。"""
        if not self.enabled:
            return self._offline_audio_services("audio_service client disabled")
        endpoints = {
            "health": "/api/health",
            "audio": "/api/audio/status",
            "sessions": "/api/audio/sessions",
            "tts": "/api/tts/status",
        }
        payloads = {}
        try:
            for key, path in endpoints.items():
                status, data = self._call("GET", path, timeout=self.status_timeout)
                if status != 200:
                    raise AudioServiceTransportError(f"{path} returned HTTP {status}")
                payloads[key] = data
        except AudioServiceTransportError as error:
            self._mark_offline(str(error))
            return self._offline_audio_services(str(error))

        tts = self._public_status(payloads["tts"])
        with self._lock:
            self._cache = tts
            self._cache_at = time.monotonic()
            self._online = True
            self._last_ok_at = time.time()
        return self._audio_services_payload(
            payloads["health"], payloads["audio"], payloads["sessions"], tts
        )

    def status(self, refresh=False):
        now = time.monotonic()
        if not refresh:
            with self._lock:
                if self._cache is not None and now - self._cache_at < self.cache_ttl:
                    return self._cache
        try:
            status, data = self._call("GET", "/api/tts/status")
        except AudioServiceTransportError as error:
            self._mark_offline(str(error))
            return self.cached_status()
        if status != 200:
            self._mark_offline(f"HTTP {status}")
            return self.cached_status()
        payload = self._public_status(data)
        with self._lock:
            self._cache = payload
            self._cache_at = time.monotonic()
            self._online = True
            self._last_ok_at = time.time()
        if data.get("limits") is None and data.get("max_text_length"):
            self.max_text_length = int(data["max_text_length"])
        return payload

    def cached_status(self):
        """非阻塞：只返回缓存，可在 WebSocket 推流路径里安全调用。"""
        with self._lock:
            if self._cache is not None:
                return self._cache
        return self._offline_status(self._last_error or "audio_service 状态尚未获取")

    # -- internals ---------------------------------------------------------
    def _mark_offline(self, reason):
        with self._lock:
            self._online = False
            self._last_error = {
                "code": "service_unavailable",
                "message": f"audio_service 不可达（{self.base_url}）",
                "detail": reason,
            }
            self._cache = self._offline_status(reason)
            self._cache_at = time.monotonic()
        LOG.warning("audio_service 不可达：%s", reason)

    def _offline_status(self, reason):
        with self._lock:
            history = list(self._local_history)[:5]
        return {
            "enabled": self.enabled,
            "online": False,
            "service": {
                "online": False,
                "url": self.base_url,
                "version": None,
                "error": {"code": "service_unavailable", "message": str(reason)},
            },
            "limits": {
                "max_text_length": self.max_text_length,
                "queue_capacity": self.queue_capacity,
            },
            "controls": {
                "voices": [],
                "emotions": [],
                "defaults": {"voice": None, "emotion": "neutral", "speech_rate": 1.0},
                "speech_rate": {"min": 0.5, "max": 2.0, "step": 0.05, "default": 1.0},
            },
            "engine": {
                "name": "audio_service",
                "available": False,
                "model": None,
                "voice": None,
                "topic": self.topic,
            },
            "provider": {"available": False, "unavailable_reason": "audio_service offline"},
            "ros": {"available": False, "topic": self.topic, "capture_rate_hz": 0.0},
            "media_audio": {"process": "media_audio", "running": None, "pid": None},
            "queue": {"pending": 0, "capacity": self.queue_capacity},
            "current": None,
            "last": history[0] if history else None,
            "history": history,
            "last_error": {"code": "service_unavailable", "message": f"audio_service 不可达（{self.base_url}）"},
            "arbitration": {"enabled": False, "stop_count": 0, "restart_count": 0},
        }

    def _offline_audio_services(self, reason):
        return {
            "online": False,
            "service": {
                "name": "audio_service",
                "url": self.base_url,
                "status": "offline",
                "version": None,
                "uptime_seconds": None,
                "error": str(reason),
            },
            "summary": {
                "capture_online": False,
                "consumer_count": 0,
                "session_count": 0,
                "tts_available": False,
                "playback_available": False,
            },
            "services": [
                self._service_card("capture", "音频输入 Gateway", "输入", "offline", "audio_service 不可达"),
                self._service_card("distribution", "PCM 分发", "网关", "offline", "audio_service 不可达"),
                self._service_card("tts", "语音合成 TTS", "输出", "offline", "audio_service 不可达"),
                self._service_card("playback", "扬声器播放桥", "输出", "offline", "audio_service 不可达"),
                self._service_card("asr", "语音识别 ASR", "处理", "not_configured", "尚未接入正式 Provider"),
                self._service_card("tse", "目标说话人提取 TSE", "处理", "not_configured", "尚未接入正式 Provider"),
            ],
            "connections": [],
            "sessions": [],
            "available_types": [],
        }

    @staticmethod
    def _service_card(service_id, name, role, status, detail, implementation=None, metrics=None):
        return {
            "id": service_id,
            "name": name,
            "role": role,
            "status": status,
            "detail": detail,
            "implementation": implementation,
            "metrics": metrics or [],
        }

    def _audio_services_payload(self, health, audio, sessions_payload, tts):
        capture = audio.get("capture") or {}
        rechunker = audio.get("rechunker") or {}
        ring = audio.get("ring_buffer") or {}
        consumers = audio.get("consumers") or []
        sessions = sessions_payload.get("items") or []
        available_types = sessions_payload.get("available_types") or []
        provider = tts.get("provider") or {}
        ros = tts.get("ros") or {}
        media = tts.get("media_audio") or {}
        queue = tts.get("queue") or {}

        capture_online = bool(audio.get("online")) and bool(capture.get("online"))
        tts_available = bool(provider.get("available"))
        playback_available = bool(ros.get("available")) and bool(media.get("running"))

        def processor_status(kind):
            matches = [item for item in sessions if kind in str(item.get("processor_type", ""))]
            if any(item.get("state") == "running" for item in matches):
                return "online", f"{sum(item.get('state') == 'running' for item in matches)} 个运行中会话"
            real_types = [name for name in available_types if kind in name and not name.startswith("mock_")]
            if real_types:
                return "idle", "Provider 已注册，当前无运行会话"
            if f"mock_{kind}" in available_types:
                return "mock", "仅 Mock Processor，尚未接入正式 Provider"
            return "not_configured", "尚未接入 Provider"

        asr_status, asr_detail = processor_status("asr")
        tse_status, tse_detail = processor_status("tse")
        services = [
            self._service_card(
                "capture", "音频输入 Gateway", "输入",
                "online" if capture_online else "offline",
                f"{capture.get('sample_rate', 0)} Hz · {capture.get('channels', 0)} 通道 · {capture.get('encoding') or '未知格式'}",
                capture.get("topic"),
                [
                    {"label": "输入频率", "value": capture.get("input_rate_hz"), "unit": "Hz"},
                    {"label": "最后帧", "value": capture.get("last_frame_age_ms"), "unit": "ms"},
                    {"label": "无效包", "value": capture.get("invalid_packets")},
                ],
            ),
            self._service_card(
                "distribution", "PCM 分发与缓冲", "网关",
                "online" if capture_online and consumers else ("idle" if capture_online else "offline"),
                f"20 ms 标准帧 · {ring.get('available_ms', 0)} ms 环形缓存",
                "/ws/audio/input",
                [
                    {"label": "输出频率", "value": rechunker.get("output_rate_hz"), "unit": "Hz"},
                    {"label": "消费者", "value": len(consumers)},
                    {"label": "累计帧", "value": rechunker.get("output_frames")},
                ],
            ),
            self._service_card(
                "tts", "语音合成 TTS", "输出",
                "online" if tts_available else "degraded",
                f"{provider.get('model') or 'Provider 未配置'} · {provider.get('voice') or '无音色'}",
                "DashScope",
                [
                    {"label": "队列", "value": queue.get("pending")},
                    {"label": "成功", "value": provider.get("successes")},
                    {"label": "失败", "value": provider.get("failures")},
                ],
            ),
            self._service_card(
                "playback", "扬声器播放桥", "输出",
                "online" if playback_available else "degraded",
                f"{ros.get('encoding') or 'PCM'} · media_audio PID {media.get('pid') or '—'}",
                ros.get("topic") or self.topic,
                [
                    {"label": "订阅者", "value": ros.get("subscribers")},
                    {"label": "已发包", "value": ros.get("published_packets")},
                    {"label": "采集频率", "value": ros.get("capture_rate_hz"), "unit": "Hz"},
                ],
            ),
            self._service_card("asr", "语音识别 ASR", "处理", asr_status, asr_detail, "Audio Session"),
            self._service_card("tse", "目标说话人提取 TSE", "处理", tse_status, tse_detail, "Audio Session"),
        ]
        return {
            "online": True,
            "service": {
                "name": "audio_service",
                "url": self.base_url,
                "status": health.get("status") or "unknown",
                "version": health.get("version") or tts.get("service", {}).get("version"),
                "uptime_seconds": health.get("uptime_seconds"),
                "error": health.get("last_error"),
            },
            "summary": {
                "capture_online": capture_online,
                "consumer_count": len(consumers),
                "session_count": len(sessions),
                "tts_available": tts_available,
                "playback_available": playback_available,
            },
            "services": services,
            "connections": consumers,
            "sessions": sessions,
            "available_types": available_types,
        }

    def _public_status(self, data):
        provider = data.get("provider") or {}
        ros = data.get("ros") or {}
        queue = data.get("queue") or {}
        recent = data.get("recent_tasks") or []
        normalized = [self._normalize_task(item) for item in recent]
        current = self._normalize_task(data.get("current_task")) if data.get("current_task") else None
        return {
            "enabled": self.enabled,
            "online": True,
            "service": {
                "online": True,
                "url": self.base_url,
                "version": data.get("version"),
                "uptime_seconds": data.get("uptime_seconds"),
                "model": data.get("model"),
                "voice": data.get("voice"),
                "last_ok_at": round(time.time(), 3),
            },
            "limits": {
                "max_text_length": self.max_text_length,
                "queue_capacity": data.get("queue_capacity") or queue.get("capacity") or self.queue_capacity,
            },
            "controls": data.get("controls") or {
                "voices": [],
                "emotions": [],
                "defaults": {"voice": data.get("voice"), "emotion": "neutral", "speech_rate": 1.0},
                "speech_rate": {"min": 0.5, "max": 2.0, "step": 0.05, "default": 1.0},
            },
            "engine": {
                "name": "audio_service",
                "available": bool(provider.get("available")) and bool(ros.get("available")),
                "model": data.get("model"),
                "voice": data.get("voice"),
                "topic": ros.get("topic") or self.topic,
                "encoding": ros.get("encoding"),
            },
            "provider": {
                "available": bool(provider.get("available")),
                "unavailable_reason": provider.get("unavailable_reason"),
                "model": provider.get("model"),
                "voice": provider.get("voice"),
                "requests": provider.get("requests"),
                "successes": provider.get("successes"),
                "failures": provider.get("failures"),
                "last_latency_ms": provider.get("last_latency_ms"),
                "last_error": provider.get("last_error"),
                "api_key_configured": provider.get("api_key_configured"),
            },
            "ros": {
                "available": bool(ros.get("available")),
                "topic": ros.get("topic"),
                "encoding": ros.get("encoding"),
                "subscribers": ros.get("subscribers"),
                "published_packets": ros.get("published_packets"),
                "capture_rate_hz": (ros.get("capture") or {}).get("rate_hz"),
                "last_error": ros.get("error"),
            },
            "media_audio": data.get("media_audio") or {},
            "queue": {
                "pending": queue.get("pending", 0),
                "capacity": queue.get("capacity", self.queue_capacity),
                "items": queue.get("items", []),
            },
            "current": current,
            "last": normalized[0] if normalized else None,
            "history": normalized,
            "last_error": data.get("last_error"),
            "playback_control": data.get("playback_control"),
            "stats": data.get("stats"),
            "arbitration": {"enabled": False, "stop_count": 0, "restart_count": 0},
        }

    def _normalize_task(self, task):
        if not task:
            return None
        error = task.get("error")
        if error:
            error = {
                "code": error.get("code"),
                "message": error.get("message"),
                "detail": _detail_text(error.get("detail")),
            }
        total = task.get("total_seconds")
        return {
            "job_id": task.get("task_id"),
            "task_id": task.get("task_id"),
            "text": task.get("text") or "",
            "text_length": task.get("text_length"),
            "phase": task.get("phase"),
            "queue_position": task.get("queue_position"),
            "client_request_id": task.get("client_request_id"),
            "voice": task.get("voice") or (task.get("audio") or {}).get("voice"),
            "emotion": task.get("emotion") or (task.get("audio") or {}).get("emotion") or "neutral",
            "speech_rate": task.get("speech_rate") or (task.get("audio") or {}).get("speech_rate") or 1.0,
            "created_at": task.get("created_at"),
            "finished_at": task.get("finished_at"),
            "elapsed_ms": round(total * 1000, 1) if total is not None else None,
            "error": error,
            "pcm": task.get("pcm") or {},
            "publish": task.get("publish") or {},
            "audio": task.get("audio") or {},
        }

    def _validate_text(self, text):
        if not isinstance(text, str):
            raise TTSRequestError("invalid_text", "text 必须是字符串")
        normalized = text.replace("\r\n", "\n").strip()
        if not normalized:
            raise TTSRequestError("empty_text", "播报文字不能为空")
        if "\x00" in normalized:
            raise TTSRequestError("invalid_text", "播报文字包含不支持的 NUL 字符")
        if len(normalized) > self.max_text_length:
            raise TTSRequestError(
                "text_too_long",
                f"播报文字超过 {self.max_text_length} 字符限制（当前 {len(normalized)}）")
        return normalized
