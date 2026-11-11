"""任务状态机与共享数据结构。

本模块不依赖 ROS / DashScope / FastAPI，便于单元测试直接构造。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional

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
TERMINAL_PHASES = frozenset(("succeeded", "failed", "cancelled"))

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9._\-]{3})[A-Za-z0-9._\-]{4,}")


def redact_secret(value: Any) -> str:
    """把文本中的 API Key 变成 sk-abc**** 形式。"""
    if value is None:
        return ""
    text = str(value)
    return _SECRET_RE.sub(lambda m: m.group(1) + "****", text)


def mask_key(key: Optional[str]) -> Optional[str]:
    """仅返回脱敏后的密钥前缀，用于状态展示。"""
    if not key:
        return None
    if len(key) <= 6:
        return "****"
    return f"{key[:6]}****"


def new_task_id() -> str:
    return uuid.uuid4().hex[:16]


class ServiceError(Exception):
    """带稳定错误码的业务异常。"""

    def __init__(self, code: str, message: str, status_code: int = 400, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}

    def as_dict(self) -> Dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


class TtsTask:
    """单个 TTS 任务。

    所有阶段切换都会记录时间戳，便于 /api/tts/history 展示耗时。
    """

    def __init__(
        self,
        text: str,
        task_id: Optional[str] = None,
        client_request_id: Optional[str] = None,
        priority: str = "normal",
        interrupt: bool = False,
        voice: Optional[str] = None,
        emotion: str = "neutral",
        speech_rate: float = 1.0,
    ):
        self.text = text
        self.task_id = task_id or new_task_id()
        self.client_request_id = client_request_id
        self.priority = priority
        self.interrupt = interrupt
        self.voice = voice
        self.emotion = emotion
        self.speech_rate = float(speech_rate)
        self.phase = "queued"
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.finished_at: Optional[float] = None
        self.timestamps: Dict[str, float] = {"queued": self.created_at}
        self.durations: Dict[str, float] = {}
        self.error: Optional[Dict[str, Any]] = None
        self.audio: Dict[str, Any] = {}
        self.pcm: Dict[str, Any] = {}
        self.publish: Dict[str, Any] = {}
        self.remaining_playback_seconds: float = 0.0
        self.deduplicated: bool = False

    # ---- 阶段控制 -------------------------------------------------
    def enter(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown phase: {phase}")
        now = time.time()
        previous = self.phase
        if previous in self.timestamps:
            self.durations[previous] = round(now - self.timestamps[previous], 4)
        self.phase = phase
        self.updated_at = now
        self.timestamps[phase] = now
        if phase in TERMINAL_PHASES:
            self.finished_at = now

    def fail(self, code: str, message: str, detail: Optional[dict] = None) -> None:
        self.error = {
            "code": code,
            "message": redact_secret(message),
            "detail": detail or {},
        }
        self.enter("failed")

    def cancel(self, reason: str = "cancelled by client") -> None:
        self.error = {"code": "cancelled", "message": reason, "detail": {}}
        self.enter("cancelled")

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def total_seconds(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return round(self.finished_at - self.created_at, 3)

    # ---- 输出 -----------------------------------------------------
    def snapshot(self, queue_position: Optional[int] = None) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "client_request_id": self.client_request_id,
            "phase": self.phase,
            "text": self.text,
            "text_length": len(self.text),
            "priority": self.priority,
            "interrupt": self.interrupt,
            "voice": self.voice,
            "emotion": self.emotion,
            "speech_rate": self.speech_rate,
            "deduplicated": self.deduplicated,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
            "finished_at": round(self.finished_at, 3) if self.finished_at else None,
            "queue_position": queue_position,
            "timestamps": {key: round(value, 3) for key, value in self.timestamps.items()},
            "durations": dict(self.durations),
            "total_seconds": self.total_seconds,
            "audio": dict(self.audio),
            "pcm": dict(self.pcm),
            "publish": dict(self.publish),
            "remaining_playback_seconds": round(self.remaining_playback_seconds, 3),
            "error": self.error,
        }

    def history_row(self) -> Dict[str, Any]:
        return self.snapshot()


class TaskHistory:
    """固定容量的任务历史。"""

    def __init__(self, max_items: int = 50):
        self.max_items = max(1, int(max_items))
        self._items: deque = deque(maxlen=self.max_items)
        self._lock = threading.Lock()

    def add(self, task: TtsTask) -> None:
        with self._lock:
            self._items.appendleft(task)

    def list(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        if limit:
            items = items[: max(1, int(limit))]
        return [task.history_row() for task in items]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class IdempotencyCache:
    """client_request_id -> task_id 的带 TTL 映射。"""

    def __init__(self, ttl_seconds: float = 600.0):
        self.ttl = max(1.0, float(ttl_seconds))
        self._items: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def get(self, client_request_id: str) -> Optional[str]:
        if not client_request_id:
            return None
        now = time.time()
        with self._lock:
            entry = self._items.get(client_request_id)
            if not entry:
                return None
            task_id, expires_at = entry
            if expires_at < now:
                self._items.pop(client_request_id, None)
                return None
            return task_id

    def put(self, client_request_id: Optional[str], task_id: str) -> None:
        if not client_request_id:
            return
        with self._lock:
            self._items[client_request_id] = (task_id, time.time() + self.ttl)

    def purge(self) -> None:
        now = time.time()
        with self._lock:
            expired = [key for key, (_, expires_at) in self._items.items() if expires_at < now]
            for key in expired:
                self._items.pop(key, None)
