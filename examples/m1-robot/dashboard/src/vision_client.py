"""Local HTTP client for vision_service.

Dashboard stays a presentation/proxy layer. It does not subscribe to vision result
topics, control the BPU directly, or run inference in the web process.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request


LOG = logging.getLogger("dashboard.vision")


class VisionServiceTransportError(Exception):
    pass


class VisionServiceClient:
    def __init__(self, config, transport=None):
        self.enabled = bool(config.get("enabled", True))
        self.base_url = str(config.get("base_url", "http://127.0.0.1:8092")).rstrip("/")
        self.timeout = float(config.get("request_timeout_seconds", 1.5))
        self.cache_ttl = float(config.get("cache_ttl_seconds", 0.8))
        self.refresh_interval = float(config.get("refresh_interval_seconds", 1.0))
        self.transport = transport or self._http_request
        self.lock = threading.RLock()
        self.cache = self._offline_payload("not_started")
        self.cache_at = 0.0
        self.stop_event = threading.Event()
        self.thread = None

    def _http_request(self, method, path, payload=None, timeout=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, method=method
        )
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, OSError) as error:
            raise VisionServiceTransportError(str(getattr(error, "reason", error)))

    def _call(self, method, path, payload=None):
        status, body = self.transport(method, path, payload, self.timeout)
        data = {}
        if body:
            try:
                data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
            except (ValueError, UnicodeDecodeError):
                data = {}
        return int(status), data

    def _offline_payload(self, reason):
        return {
            "status": "offline",
            "online": False,
            "service_url": self.base_url,
            "error": reason,
            "processors": [],
        }

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._refresh_loop, name="vision-status-refresh", daemon=True
        )
        self.thread.start()
        LOG.info("vision_service client started, base_url=%s", self.base_url)

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.thread = None

    def _refresh_loop(self):
        while not self.stop_event.is_set():
            self.status(refresh=True)
            self.stop_event.wait(self.refresh_interval)

    def status(self, refresh=False):
        if not self.enabled:
            return self._offline_payload("disabled")
        with self.lock:
            if not refresh and time.monotonic() - self.cache_at <= self.cache_ttl:
                return dict(self.cache)
        try:
            status, data = self._call("GET", "/api/vision/status")
            if status != 200:
                result = self._offline_payload(f"HTTP {status}")
            else:
                result = dict(data)
                result["online"] = True
                result["service_url"] = self.base_url
        except VisionServiceTransportError as error:
            result = self._offline_payload(str(error))
        with self.lock:
            self.cache = result
            self.cache_at = time.monotonic()
            return dict(result)

    def cached_status(self):
        with self.lock:
            return dict(self.cache)

    def latest_detection(self):
        try:
            return self._call("GET", "/api/vision/detections/latest")
        except VisionServiceTransportError as error:
            return 503, {
                "available": False,
                "stale": True,
                "error": {"code": "vision_service_unavailable", "message": str(error)},
            }

    def latest_tracks(self):
        try:
            return self._call("GET", "/api/vision/tracks/latest")
        except VisionServiceTransportError as error:
            return 503, {
                "available": False,
                "stale": True,
                "error": {"code": "vision_service_unavailable", "message": str(error)},
            }

    def control_detection(self, action):
        if action not in ("start", "stop"):
            raise ValueError("action must be start or stop")
        try:
            status, data = self._call(
                "POST", f"/api/vision/processors/object_detection/{action}", {}
            )
        except VisionServiceTransportError as error:
            return 503, {
                "ok": False,
                "error": {"code": "vision_service_unavailable", "message": str(error)},
            }
        self.status(refresh=True)
        return status, data
