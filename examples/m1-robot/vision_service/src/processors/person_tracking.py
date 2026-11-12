import math
import threading
import time
from collections import deque

from processors.base import BaseProcessor, ProcessorError


def _center(box):
    return ((box["x1"] + box["x2"]) / 2.0, (box["y1"] + box["y2"]) / 2.0)


def _iou(left, right):
    x1 = max(left["x1"], right["x1"])
    y1 = max(left["y1"], right["y1"])
    x2 = min(left["x2"], right["x2"])
    y2 = min(left["y2"], right["y2"])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left["x2"] - left["x1"]) * max(0.0, left["y2"] - left["y1"])
    right_area = max(0.0, right["x2"] - right["x1"]) * max(0.0, right["y2"] - right["y1"])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _clamp_box(box):
    x1 = max(0.0, min(1.0, float(box["x1"])))
    y1 = max(0.0, min(1.0, float(box["y1"])))
    x2 = max(x1, min(1.0, float(box["x2"])))
    y2 = max(y1, min(1.0, float(box["y2"])))
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _smooth_box(previous, current, alpha):
    return {
        key: previous[key] * (1.0 - alpha) + current[key] * alpha
        for key in ("x1", "y1", "x2", "y2")
    }


class PersonTrackingProcessor(BaseProcessor):
    """Dependency-free short-term person tracker over normalized detection boxes."""

    def __init__(self, config, result_bus):
        super().__init__("person_tracking", config)
        self.enabled = bool(config.get("enabled", True))
        self.auto_start = bool(config.get("auto_start", True))
        self.iou_threshold = float(config.get("iou_threshold", 0.2))
        self.center_distance_threshold = float(config.get("center_distance_threshold", 0.18))
        self.smoothing_alpha = float(config.get("smoothing_alpha", 0.65))
        self.velocity_alpha = float(config.get("velocity_alpha", 0.5))
        self.min_confirmed_hits = max(1, int(config.get("min_confirmed_hits", 2)))
        self.max_missed_frames = max(1, int(config.get("max_missed_frames", 10)))
        self.direction_threshold = float(config.get("direction_threshold", 0.04))
        self.result_bus = result_bus
        self.tracks = {}
        self.next_track_id = 1
        self.input_frames = 0
        self.output_frames = 0
        self.created_tracks = 0
        self.removed_tracks = 0
        self.id_switches = 0
        self.last_input_at = None
        self.last_input_monotonic = None
        self.last_result_at = None
        self.input_times = deque(maxlen=300)
        self.output_times = deque(maxlen=300)
        self.lock = threading.RLock()

    def start(self):
        if not self.enabled:
            raise ProcessorError("person_tracking is disabled by configuration")
        with self.lock:
            if self.state == "running":
                return self.status()
            self._set_state("starting")
            self._set_state("running")
            return self.status()

    def stop(self):
        with self.lock:
            self.reset()
            self._set_state("stopped")
            return self.status()

    def reset(self):
        with self.lock:
            self.tracks.clear()

    @staticmethod
    def _rate(samples, now):
        while samples and now - samples[0] > 10.0:
            samples.popleft()
        if len(samples) < 2:
            return 0.0
        duration = samples[-1] - samples[0]
        return round((len(samples) - 1) / duration, 2) if duration > 0 else 0.0

    def _predict_box(self, track, now):
        dt = max(0.0, min(1.0, now - track["last_seen_monotonic"]))
        vx, vy = track["velocity_x"], track["velocity_y"]
        return _clamp_box({
            "x1": track["bbox"]["x1"] + vx * dt,
            "y1": track["bbox"]["y1"] + vy * dt,
            "x2": track["bbox"]["x2"] + vx * dt,
            "y2": track["bbox"]["y2"] + vy * dt,
        })

    def _match(self, detections, now):
        candidates = []
        for track_id, track in self.tracks.items():
            predicted = self._predict_box(track, now)
            predicted_center = _center(predicted)
            for detection_index, detection in enumerate(detections):
                box = detection["bbox_normalized"]
                overlap = _iou(predicted, box)
                center = _center(box)
                distance = math.hypot(center[0] - predicted_center[0], center[1] - predicted_center[1])
                if overlap < self.iou_threshold and distance > self.center_distance_threshold:
                    continue
                distance_score = max(0.0, 1.0 - distance / max(self.center_distance_threshold, 1e-6))
                score = overlap * 0.75 + distance_score * 0.25
                candidates.append((score, track_id, detection_index))
        candidates.sort(reverse=True)
        matches = []
        used_tracks = set()
        used_detections = set()
        for _score, track_id, detection_index in candidates:
            if track_id in used_tracks or detection_index in used_detections:
                continue
            matches.append((track_id, detection_index))
            used_tracks.add(track_id)
            used_detections.add(detection_index)
        return matches, used_tracks, used_detections

    def _new_track(self, detection, now_wall, now_mono):
        track_id = self.next_track_id
        self.next_track_id += 1
        box = _clamp_box(detection["bbox_normalized"])
        track = {
            "track_id": track_id,
            "bbox": box,
            "confidence": float(detection.get("confidence", 0.0)),
            "created_at": now_wall,
            "last_seen_at": now_wall,
            "last_seen_monotonic": now_mono,
            "hits": 1,
            "age_frames": 1,
            "missed_frames": 0,
            "state": "confirmed" if self.min_confirmed_hits <= 1 else "tentative",
            "velocity_x": 0.0,
            "velocity_y": 0.0,
            "entered_emitted": self.min_confirmed_hits <= 1,
        }
        self.tracks[track_id] = track
        self.created_tracks += 1
        return track

    def _update_track(self, track, detection, now_wall, now_mono):
        current = _clamp_box(detection["bbox_normalized"])
        old_center = _center(track["bbox"])
        new_center = _center(current)
        dt = max(1e-3, now_mono - track["last_seen_monotonic"])
        measured_vx = (new_center[0] - old_center[0]) / dt
        measured_vy = (new_center[1] - old_center[1]) / dt
        velocity_alpha = max(0.0, min(1.0, self.velocity_alpha))
        track["velocity_x"] = track["velocity_x"] * (1.0 - velocity_alpha) + measured_vx * velocity_alpha
        track["velocity_y"] = track["velocity_y"] * (1.0 - velocity_alpha) + measured_vy * velocity_alpha
        track["bbox"] = _smooth_box(track["bbox"], current, max(0.0, min(1.0, self.smoothing_alpha)))
        track["confidence"] = float(detection.get("confidence", track["confidence"]))
        track["last_seen_at"] = now_wall
        track["last_seen_monotonic"] = now_mono
        track["hits"] += 1
        track["age_frames"] += 1
        track["missed_frames"] = 0
        if track["hits"] >= self.min_confirmed_hits:
            track["state"] = "confirmed"

    def _direction(self, track):
        vx, vy = track["velocity_x"], track["velocity_y"]
        if max(abs(vx), abs(vy)) < self.direction_threshold:
            return "stationary"
        if abs(vx) >= abs(vy):
            return "right" if vx > 0 else "left"
        return "down" if vy > 0 else "up"

    def _public_track(self, track, width, height):
        box = _clamp_box(track["bbox"])
        pixel = {
            "x1": round(box["x1"] * width, 3),
            "y1": round(box["y1"] * height, 3),
            "x2": round(box["x2"] * width, 3),
            "y2": round(box["y2"] * height, 3),
        }
        pixel["width"] = round(pixel["x2"] - pixel["x1"], 3)
        pixel["height"] = round(pixel["y2"] - pixel["y1"], 3)
        return {
            "id": f"person:{track['track_id']}",
            "track_id": track["track_id"],
            "class_id": 0,
            "label": "person",
            "source_label": "body",
            "track_state": track["state"],
            "confidence": round(track["confidence"], 6),
            "bbox": pixel,
            "bbox_normalized": {key: round(value, 6) for key, value in box.items()},
            "age_frames": track["age_frames"],
            "age_seconds": round(max(0.0, track["last_seen_at"] - track["created_at"]), 3),
            "missed_frames": track["missed_frames"],
            "velocity_normalized_per_second": {
                "x": round(track["velocity_x"], 6),
                "y": round(track["velocity_y"], 6),
            },
            "velocity": {
                "x": round(track["velocity_x"], 6),
                "y": round(track["velocity_y"], 6),
                "unit": "normalized_per_second",
            },
            "direction": self._direction(track),
        }

    def process_frame(self, frame):
        now_wall = float(frame.get("received_at", time.time()))
        now_mono = float(frame.get("received_monotonic", time.monotonic()))
        with self.lock:
            if self.state != "running":
                return None
            detections = [
                item for item in frame.get("detections", [])
                if item.get("label") == "person" and isinstance(item.get("bbox_normalized"), dict)
            ]
            self.input_frames += 1
            self.last_input_at = now_wall
            self.last_input_monotonic = now_mono
            self.input_times.append(now_mono)
            matches, matched_tracks, matched_detections = self._match(detections, now_mono)
            events = []

            for track_id, detection_index in matches:
                track = self.tracks[track_id]
                was_confirmed = track["state"] == "confirmed"
                self._update_track(track, detections[detection_index], now_wall, now_mono)
                if not was_confirmed and track["state"] == "confirmed" and not track["entered_emitted"]:
                    events.append({"type": "person_entered", "track_id": track_id, "timestamp": now_wall})
                    track["entered_emitted"] = True

            for track_id, track in list(self.tracks.items()):
                if track_id in matched_tracks:
                    continue
                track["age_frames"] += 1
                track["missed_frames"] += 1
                if track["state"] == "confirmed":
                    track["state"] = "lost"
                if track["missed_frames"] > self.max_missed_frames:
                    if track["entered_emitted"]:
                        events.append({"type": "person_left", "track_id": track_id, "timestamp": now_wall})
                    del self.tracks[track_id]
                    self.removed_tracks += 1

            for detection_index, detection in enumerate(detections):
                if detection_index in matched_detections:
                    continue
                track = self._new_track(detection, now_wall, now_mono)
                if track["entered_emitted"]:
                    events.append({"type": "person_entered", "track_id": track["track_id"], "timestamp": now_wall})

            public_tracks = [
                self._public_track(track, frame["source_width"], frame["source_height"])
                for track in sorted(self.tracks.values(), key=lambda item: item["track_id"])
            ]
            output = {
                "type": "tracking_frame",
                "processor": "person_tracking",
                "source": "object_detection",
                "model": frame.get("model"),
                "source_frame_sequence": frame.get("source_frame_sequence"),
                "source_timestamp": frame.get("source_timestamp"),
                "received_at": now_wall,
                "received_monotonic": now_mono,
                "source_width": frame["source_width"],
                "source_height": frame["source_height"],
                "detections": public_tracks,
                "tracks": public_tracks,
                "detection_count": len(public_tracks),
                "track_count": len(public_tracks),
                "confirmed_track_count": sum(item["track_state"] == "confirmed" for item in public_tracks),
                "events": events,
                "metadata": {
                    "source_detection_count": len(detections),
                    "active_tracks": len(self.tracks),
                    "created_tracks": self.created_tracks,
                    "removed_tracks": self.removed_tracks,
                },
            }
            published = self.result_bus.publish(output)
            self.output_frames += 1
            self.last_result_at = now_wall
            self.output_times.append(now_mono)
            return published

    def status(self):
        now = time.monotonic()
        with self.lock:
            base = super().status()
            source_age = None if self.last_input_monotonic is None else max(0.0, now - self.last_input_monotonic)
            base.update({
                "enabled": self.enabled,
                "auto_start": self.auto_start,
                "source": "object_detection",
                "result_type": "tracking_frame",
                "algorithm": "iou_center_motion",
                "source_online": source_age is not None and source_age <= 1.5,
                "source_age_seconds": round(source_age, 3) if source_age is not None else None,
                "active_tracks": len(self.tracks),
                "confirmed_tracks": sum(item["state"] == "confirmed" for item in self.tracks.values()),
                "created_tracks": self.created_tracks,
                "removed_tracks": self.removed_tracks,
                "input_frames": self.input_frames,
                "input_messages": self.input_frames,
                "output_frames": self.output_frames,
                "parse_errors": 0,
                "input_rate_hz": self._rate(self.input_times, now),
                "output_rate_hz": self._rate(self.output_times, now),
                "last_input_at": self.last_input_at,
                "last_result_at": self.last_result_at,
                "max_missed_frames": self.max_missed_frames,
                "min_confirmed_hits": self.min_confirmed_hits,
            })
            return base
