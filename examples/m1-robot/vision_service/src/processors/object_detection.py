import json
import math
import time
from collections import deque

from processors.base import BaseProcessor, ProcessorError


BOX_TYPES = (("body_boxes", 0, "person", "body"),)
IGNORED_BOX_TYPES = ("head_boxes", "face_boxes", "hand_boxes")


def _finite_number(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} is not numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} is not finite")
    return number


def parse_multitask_result(raw, confidence_threshold=0.5, received_at=None,
                           received_monotonic=None, model=None):
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError(f"invalid multitask JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("multitask result must be an object")

    width = int(payload.get("model_input_width", 0))
    height = int(payload.get("model_input_height", 0))
    if width <= 0 or height <= 0:
        raise ValueError("model input dimensions must be positive")
    frame_sequence = int(payload.get("frame_seq", 0))
    received_at = time.time() if received_at is None else float(received_at)
    received_monotonic = time.monotonic() if received_monotonic is None else float(received_monotonic)
    threshold = max(0.0, min(1.0, float(confidence_threshold)))
    detections = []
    rejected = 0

    for field, class_id, label, source_label in BOX_TYPES:
        boxes = payload.get(field, [])
        if not isinstance(boxes, list):
            rejected += 1
            continue
        for index, box in enumerate(boxes):
            if not isinstance(box, dict):
                rejected += 1
                continue
            try:
                confidence = _finite_number(box.get("confidence"), "confidence")
                x1 = _finite_number(box.get("x_offset"), "x_offset")
                y1 = _finite_number(box.get("y_offset"), "y_offset")
                box_width = _finite_number(box.get("width"), "width")
                box_height = _finite_number(box.get("height"), "height")
            except ValueError:
                rejected += 1
                continue
            if confidence < threshold or box_width <= 0 or box_height <= 0:
                continue
            x1 = max(0.0, min(float(width), x1))
            y1 = max(0.0, min(float(height), y1))
            x2 = max(0.0, min(float(width), x1 + box_width))
            y2 = max(0.0, min(float(height), y1 + box_height))
            if x2 <= x1 or y2 <= y1:
                rejected += 1
                continue
            bbox = {
                "x1": round(x1, 3), "y1": round(y1, 3),
                "x2": round(x2, 3), "y2": round(y2, 3),
                "width": round(x2 - x1, 3), "height": round(y2 - y1, 3),
            }
            normalized = {
                "x1": round(x1 / width, 6), "y1": round(y1 / height, 6),
                "x2": round(x2 / width, 6), "y2": round(y2 / height, 6),
            }
            detections.append({
                "id": f"{frame_sequence}:{source_label}:{index}",
                "class_id": class_id,
                "label": label,
                "source_label": source_label,
                "confidence": round(max(0.0, min(1.0, confidence)), 6),
                "bbox": bbox,
                "bbox_normalized": normalized,
            })

    return {
        "type": "detection_frame",
        "processor": "object_detection",
        "source": "vendor_multitask",
        "model": model,
        "source_frame_sequence": frame_sequence,
        "source_timestamp": None,
        "received_at": round(received_at, 6),
        "received_monotonic": received_monotonic,
        "source_width": width,
        "source_height": height,
        "confidence_threshold": threshold,
        "detections": detections,
        "detection_count": len(detections),
        "metadata": {
            "body_count": int(payload.get("body_count", 0) or 0),
            "ignored_source_boxes": sum(
                len(payload.get(field, []))
                for field in IGNORED_BOX_TYPES
                if isinstance(payload.get(field, []), list)
            ),
            "rejected_boxes": rejected,
        },
    }


class ObjectDetectionProcessor(BaseProcessor):
    def __init__(self, config, result_bus, ros_runtime, on_frame=None):
        super().__init__("object_detection", config)
        self.result_bus = result_bus
        self.ros_runtime = ros_runtime
        self.enabled = bool(config.get("enabled", True))
        self.auto_start = bool(config.get("auto_start", False))
        self.manage_source_enable = bool(config.get("manage_source_enable", True))
        self.threshold = float(config.get("confidence_threshold", 0.5))
        self.source_stale_after = float(config.get("source_stale_after_seconds", 1.0))
        self.warmup_seconds = float(config.get("warmup_seconds", 3.0))
        self.probe_seconds = float(config.get("probe_existing_stream_seconds", 0.75))
        self.model = config.get("model")
        self.on_frame = on_frame
        self.owns_source_enable = False
        self.last_raw_at = None
        self.last_raw_monotonic = None
        self.last_result_at = None
        self.last_result_monotonic = None
        self.input_messages = 0
        self.output_frames = 0
        self.parse_errors = 0
        self.input_times = deque(maxlen=300)
        self.output_times = deque(maxlen=300)

    def start(self):
        if not self.enabled:
            raise ProcessorError("object_detection is disabled by configuration")
        with self.lock:
            if self.state in ("starting", "running", "degraded"):
                return self.status()
            self._set_state("starting")

        try:
            if self.manage_source_enable and not self._source_recent():
                deadline = time.monotonic() + max(0.0, self.probe_seconds)
                while time.monotonic() < deadline and not self._source_recent():
                    time.sleep(0.05)
                if not self._source_recent():
                    self.ros_runtime.set_detection_enabled(True)
                    self.owns_source_enable = True
            self._set_state("running")
            return self.status()
        except Exception as error:
            self._set_state("failed", {
                "code": "start_failed", "message": str(error),
            })
            raise

    def stop(self):
        with self.lock:
            if self.state == "stopped":
                return self.status()
            try:
                if self.owns_source_enable:
                    self.ros_runtime.set_detection_enabled(False)
                    self.owns_source_enable = False
                    self.last_raw_monotonic = None
                self._set_state("stopped")
                return self.status()
            except Exception as error:
                self._set_state("failed", {
                    "code": "stop_failed", "message": str(error),
                })
                raise

    def _source_recent(self):
        return (self.last_raw_monotonic is not None and
                time.monotonic() - self.last_raw_monotonic <= self.source_stale_after)

    def handle_raw_result(self, raw, received_at=None, received_monotonic=None):
        received_at = time.time() if received_at is None else received_at
        received_monotonic = time.monotonic() if received_monotonic is None else received_monotonic
        with self.lock:
            self.last_raw_at = received_at
            self.last_raw_monotonic = received_monotonic
            self.input_messages += 1
            self.input_times.append(received_monotonic)
            active = self.state in ("starting", "running", "degraded")
        if not active:
            return None
        try:
            event = parse_multitask_result(
                raw,
                confidence_threshold=self.threshold,
                received_at=received_at,
                received_monotonic=received_monotonic,
                model=self.model,
            )
        except ValueError as error:
            with self.lock:
                self.parse_errors += 1
                self.last_error = {"code": "invalid_source_result", "message": str(error)}
            return None
        published = self.result_bus.publish(event)
        if self.on_frame is not None:
            self.on_frame(published)
        with self.lock:
            self.last_result_at = received_at
            self.last_result_monotonic = received_monotonic
            self.output_frames += 1
            self.output_times.append(received_monotonic)
            self.last_error = None
        return published

    @staticmethod
    def _rate(samples, now):
        while samples and now - samples[0] > 10.0:
            samples.popleft()
        if len(samples) < 2:
            return 0.0
        duration = samples[-1] - samples[0]
        return round((len(samples) - 1) / duration, 2) if duration > 0 else 0.0

    def status(self):
        now = time.monotonic()
        with self.lock:
            reported_state = self.state
            source_age = None
            if self.last_raw_monotonic is not None:
                source_age = max(0.0, now - self.last_raw_monotonic)
            if reported_state == "running" and self.started_at is not None:
                runtime = time.time() - self.started_at
                if runtime > self.warmup_seconds and not self._source_recent():
                    reported_state = "degraded"
            base = super().status()
            base.update({
                "state": reported_state,
                "enabled": self.enabled,
                "auto_start": self.auto_start,
                "source": self.config.get("source", "vendor_multitask"),
                "result_topic": self.config.get("result_topic"),
                "enable_topic": self.config.get("enable_topic"),
                "manage_source_enable": self.manage_source_enable,
                "owns_source_enable": self.owns_source_enable,
                "model": self.model,
                "confidence_threshold": self.threshold,
                "source_online": source_age is not None and source_age <= self.source_stale_after,
                "source_age_seconds": round(source_age, 3) if source_age is not None else None,
                "last_raw_at": self.last_raw_at,
                "last_result_at": self.last_result_at,
                "input_messages": self.input_messages,
                "output_frames": self.output_frames,
                "parse_errors": self.parse_errors,
                "input_rate_hz": self._rate(self.input_times, now),
                "output_rate_hz": self._rate(self.output_times, now),
            })
            return base
