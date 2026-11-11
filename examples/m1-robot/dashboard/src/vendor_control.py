"""HTTP client for the vendor motion-control service on 127.0.0.1:12321.

The vendor service is intentionally addressed through loopback only.  The
Dashboard backend is the single place that talks to it; browsers never do.
"""

import http.client
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass


LOG = logging.getLogger("dashboard.vendor")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 12321
DEFAULT_CONNECT_TIMEOUT = 1.2
DEFAULT_READ_TIMEOUT = 3.0
MAX_RAW_BODY = 2048


class VendorControlError(Exception):
    """Base class for every normalized vendor failure."""

    code = "vendor_error"

    def __init__(self, message, request_id=None, submitted_at=None, responded_at=None,
                 latency_ms=None, http_status=None, raw_body="", detail=None):
        super().__init__(message)
        self.message = message
        self.request_id = request_id
        self.submitted_at = submitted_at
        self.responded_at = responded_at
        self.latency_ms = latency_ms
        self.http_status = http_status
        self.raw_body = (raw_body or "")[:MAX_RAW_BODY]
        self.detail = detail

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "request_id": self.request_id,
            "submitted_at": self.submitted_at,
            "responded_at": self.responded_at,
            "latency_ms": self.latency_ms,
            "http_status": self.http_status,
            "raw_body": self.raw_body,
            "detail": self.detail,
        }


class VendorConnectionError(VendorControlError):
    code = "connection_failed"


class VendorTimeoutError(VendorControlError):
    code = "timed_out"


class VendorHttpError(VendorControlError):
    code = "vendor_http_error"


class VendorProtocolError(VendorControlError):
    code = "vendor_protocol_error"


@dataclass
class VendorResponse:
    request_id: str
    method: str
    path: str
    submitted_at: float
    responded_at: float
    latency_ms: float
    http_status: int
    content_type: str
    body: object
    raw_body: str

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "method": self.method,
            "path": self.path,
            "submitted_at": self.submitted_at,
            "responded_at": self.responded_at,
            "latency_ms": self.latency_ms,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "body": self.body,
            "raw_body": self.raw_body,
        }


class VendorControlClient:
    """Thread-safe, loopback-only client for ``utils_webctl_plugin``.

    Each call opens its own connection so a slow/broken command can never
    block the high-priority stop path.
    """

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT, read_timeout=DEFAULT_READ_TIMEOUT):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    # -- public read-only endpoints -------------------------------------
    def health(self, request_id=None):
        return self._request("GET", "/", None, request_id)

    def battery(self, request_id=None):
        return self._request("GET", "/api/battery", None, request_id, expect_json=True)

    def status(self, request_id=None):
        return self._request("GET", "/api/status", None, request_id, expect_json=True)

    # -- control endpoints ----------------------------------------------
    def send_command(self, command, request_id=None):
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        return self._request("POST", "/api/control", {"command": command}, request_id,
                             expect_json=True)

    def emergency_stop(self, request_id=None):
        return self._request("POST", "/api/emergency_stop", {}, request_id, expect_json=True)

    def reset_emergency(self, request_id=None):
        return self._request("POST", "/api/reset_emergency", {}, request_id, expect_json=True)

    # -- transport ------------------------------------------------------
    def _request(self, method, path, payload, request_id=None, expect_json=False):
        request_id = request_id or str(uuid.uuid4())
        submitted_at = time.time()
        started = time.monotonic()
        raw_body = ""
        content_type = ""
        status = None
        body_bytes = b""

        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.connect_timeout)
        try:
            try:
                conn.connect()
            except socket.timeout as error:
                raise VendorTimeoutError(
                    f"connect timeout after {self.connect_timeout:.1f}s",
                    request_id=request_id, submitted_at=submitted_at,
                    responded_at=time.time(), latency_ms=(time.monotonic() - started) * 1000,
                    detail=str(error),
                )
            except OSError as error:
                raise VendorConnectionError(
                    f"cannot connect to vendor service: {error}",
                    request_id=request_id, submitted_at=submitted_at,
                    responded_at=time.time(), latency_ms=(time.monotonic() - started) * 1000,
                    detail=str(error),
                )

            try:
                if conn.sock is not None:
                    conn.sock.settimeout(self.read_timeout)
                headers = {"Accept": "application/json"}
                encoded = None
                if payload is not None:
                    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                conn.request(method, path, body=encoded, headers=headers)
                response = conn.getresponse()
                status = response.status
                content_type = response.getheader("Content-Type") or ""
                body_bytes = response.read()
            except socket.timeout as error:
                raise VendorTimeoutError(
                    f"read timeout after {self.read_timeout:.1f}s",
                    request_id=request_id, submitted_at=submitted_at,
                    responded_at=time.time(), latency_ms=(time.monotonic() - started) * 1000,
                    http_status=status, detail=str(error),
                )
            except OSError as error:
                raise VendorConnectionError(
                    f"vendor request failed: {error}",
                    request_id=request_id, submitted_at=submitted_at,
                    responded_at=time.time(), latency_ms=(time.monotonic() - started) * 1000,
                    http_status=status, detail=str(error),
                )
        finally:
            try:
                conn.close()
            except Exception:
                pass

        responded_at = time.time()
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        charset = "utf-8"
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            raw_body = body_bytes.decode(charset, errors="replace")
        except LookupError:
            raw_body = body_bytes.decode("utf-8", errors="replace")

        body = None
        if expect_json or "json" in content_type.lower():
            try:
                body = json.loads(raw_body) if raw_body.strip() else None
            except json.JSONDecodeError as error:
                raise VendorProtocolError(
                    f"invalid JSON response: {error}",
                    request_id=request_id, submitted_at=submitted_at, responded_at=responded_at,
                    latency_ms=latency_ms, http_status=status, raw_body=raw_body,
                )

        stored_body = raw_body
        if not expect_json and len(stored_body) > 256:
            stored_body = stored_body[:256] + "…"

        if status >= 400:
            raise VendorHttpError(
                f"vendor returned HTTP {status}",
                request_id=request_id, submitted_at=submitted_at, responded_at=responded_at,
                latency_ms=latency_ms, http_status=status, raw_body=raw_body,
                detail={"body": body},
            )

        if expect_json and isinstance(body, dict) and body.get("status") == "error":
            raise VendorHttpError(
                f"vendor rejected request: {body.get('message', 'unknown error')}",
                request_id=request_id, submitted_at=submitted_at, responded_at=responded_at,
                latency_ms=latency_ms, http_status=status, raw_body=raw_body,
                detail={"body": body},
            )

        return VendorResponse(
            request_id=request_id,
            method=method,
            path=path,
            submitted_at=submitted_at,
            responded_at=responded_at,
            latency_ms=latency_ms,
            http_status=status,
            content_type=content_type,
            body=body,
            raw_body=stored_body,
        )
