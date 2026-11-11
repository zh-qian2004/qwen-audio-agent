"""ASR/TSE/VAD 等处理会话的共享状态。"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

STATES = ("pending", "running", "finished", "cancelled", "gapped", "failed")


class AudioSession:
    def __init__(
        self,
        processor_type: str,
        pre_roll_ms: int = 0,
        policy: str = "fail_on_gap",
        capacity: int = 100,
        params: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ):
        self.session_id = session_id or f"{processor_type}-{uuid.uuid4().hex[:10]}"
        self.processor_type = processor_type
        self.policy = policy
        self.capacity = int(capacity)
        self.params = dict(params or {})
        self.pre_roll_ms = int(pre_roll_ms)
        self.pre_roll_frames = 0

        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.state = "pending"

        self.input_sequence_start: Optional[int] = None
        self.input_sequence_end: Optional[int] = None
        self.received_frames = 0
        self.dropped_frames = 0
        self.gap_detected = False
        self.error: Optional[Dict[str, Any]] = None
        self.stop_requested = False

    # ---- 状态迁移 -------------------------------------------------
    def mark_started(self) -> None:
        self.state = "running"
        self.started_at = time.time()

    def note_frame(self, sequence: int) -> None:
        if self.input_sequence_start is None:
            self.input_sequence_start = sequence
        self.input_sequence_end = sequence
        self.received_frames += 1

    def mark_finished(self) -> None:
        self.state = "finished"
        self.finished_at = time.time()

    def mark_cancelled(self, reason: str = "cancelled") -> None:
        self.state = "cancelled"
        self.finished_at = time.time()
        self.error = {"code": "cancelled", "message": reason}

    def mark_gap(self, event: Dict[str, Any]) -> None:
        self.state = "gapped"
        self.gap_detected = True
        self.finished_at = time.time()
        self.error = {
            "code": "audio_gap",
            "message": event.get("reason"),
            "detail": event,
        }

    def mark_failed(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        self.state = "failed"
        self.finished_at = time.time()
        self.error = {"code": code, "message": message, "detail": detail or {}}

    # ---- 输出 -----------------------------------------------------
    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return round(end - self.started_at, 3)

    def snapshot(self, processor_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "processor_type": self.processor_type,
            "state": self.state,
            "policy": self.policy,
            "capacity": self.capacity,
            "pre_roll_ms": self.pre_roll_ms,
            "pre_roll_frames": self.pre_roll_frames,
            "created_at": round(self.created_at, 3),
            "started_at": round(self.started_at, 3) if self.started_at else None,
            "finished_at": round(self.finished_at, 3) if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "input_sequence_start": self.input_sequence_start,
            "input_sequence_end": self.input_sequence_end,
            "received_frames": self.received_frames,
            "dropped_frames": self.dropped_frames,
            "gap_detected": self.gap_detected,
            "error": self.error,
            "processor": processor_status,
        }
