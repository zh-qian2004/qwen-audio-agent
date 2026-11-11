"""Ephemeral Audio Agent heartbeat state for the Dashboard."""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Dict, Optional


class AudioAgentStatus:
    """Keep the latest connector heartbeat without persisting runtime details."""

    def __init__(self, stale_after_seconds: float = 6.0):
        self.stale_after_seconds = max(1.0, float(stale_after_seconds))
        self._lock = threading.Lock()
        self._payload: Optional[Dict[str, Any]] = None
        self._received_at: Optional[float] = None
        self._received_monotonic: Optional[float] = None

    def update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._payload = copy.deepcopy(payload)
            self._received_at = time.time()
            self._received_monotonic = time.monotonic()
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            payload = copy.deepcopy(self._payload)
            received_at = self._received_at
            received_monotonic = self._received_monotonic

        if payload is None or received_monotonic is None:
            return self._empty("offline", "尚未收到 Audio Agent 心跳")

        age = max(0.0, time.monotonic() - received_monotonic)
        stale = age > self.stale_after_seconds
        connector = payload.get("connector") or {}
        gateway_component = connector.get("gateway") or {}
        gateway = payload.get("gateway") or {}
        gateway_state = str(gateway_component.get("state") or "unknown")
        gateway_ready = gateway.get("status") == "ready" and gateway_state in {
            "ready", "connected", "online",
        }
        state = "stale" if stale else ("online" if gateway_ready else "degraded")
        error = "Audio Agent 心跳已过期" if stale else gateway_component.get("error")
        return {
            "online": state == "online",
            "state": state,
            "last_seen_at": round(received_at, 3) if received_at else None,
            "age_seconds": round(age, 3),
            "stale_after_seconds": self.stale_after_seconds,
            "instance_id": payload.get("instance_id"),
            "label": payload.get("label") or "M1 Audio Agent",
            "connector": connector,
            "gateway": gateway,
            "error": error,
        }

    def service_card(self) -> Dict[str, Any]:
        status = self.snapshot()
        gateway = status.get("gateway") or {}
        realtime = gateway.get("realtime") or {}
        backend = gateway.get("backend") or {}
        voice_clients = gateway.get("voice_clients") or {}
        front_model = realtime.get("model") or realtime.get("provider") or "未连接"
        backend_model = backend.get("model") or backend.get("label") or backend.get("kind") or "未配置"
        return {
            "id": "audio_agent",
            "name": "Audio Agent",
            "role": "智能交互",
            "status": status["state"],
            "detail": f"前台 {front_model} · 后台 {backend_model}",
            "implementation": status.get("label") or "Qwen Audio Agent",
            "metrics": [
                {"label": "Gateway", "value": (status.get("connector", {}).get("gateway") or {}).get("state") or "—"},
                {"label": "语音客户端", "value": voice_clients.get("connected")},
                {"label": "心跳延迟", "value": status.get("age_seconds"), "unit": "s"},
            ],
        }

    def _empty(self, state: str, error: str) -> Dict[str, Any]:
        return {
            "online": False,
            "state": state,
            "last_seen_at": None,
            "age_seconds": None,
            "stale_after_seconds": self.stale_after_seconds,
            "instance_id": None,
            "label": "M1 Audio Agent",
            "connector": {},
            "gateway": {},
            "error": error,
        }
