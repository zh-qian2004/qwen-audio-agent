"""Lightweight playback-aware acoustic echo suppression.

The output service and the isolated microphone service are separate processes.
The output process therefore sends a low-rate 16 kHz mono reference over a
loopback UDP socket.  The input process aligns that reference with captured
20 ms microphone frames and attenuates frames dominated by playback echo.

This is deliberately a double-talk gate, not a full adaptive echo canceller:
near-end speech is allowed through after a short confirmation window so
full-duplex barge-in remains available.
"""
from __future__ import annotations

import audioop
import math
import socket
import struct
import threading
import time
from array import array
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterable, Optional, Tuple


MAGIC = b"M1ER"
PACKET_PCM = 1
PACKET_END = 2
HEADER = struct.Struct("!4sBQI")
PCM_RATE = 16_000
PCM_WIDTH = 2
FRAME_MS = 20
FRAME_BYTES = PCM_RATE * PCM_WIDTH * FRAME_MS // 1000


def _db_ratio(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


def _pcm_rms(pcm: bytes) -> float:
    return float(audioop.rms(pcm, PCM_WIDTH)) if pcm else 0.0


def _samples(pcm: bytes) -> array:
    values = array("h")
    values.frombytes(pcm)
    if struct.pack("=H", 1) != struct.pack("<H", 1):  # pragma: no cover - robot is little endian
        values.byteswap()
    return values


def _correlation_and_residual(mic_pcm: bytes, reference_pcm: bytes) -> Tuple[float, float, float]:
    """Return absolute normalized correlation, least-squares gain and residual RMS."""
    mic = _samples(mic_pcm)
    reference = _samples(reference_pcm)
    count = min(len(mic), len(reference))
    if count <= 1:
        return 0.0, 0.0, _pcm_rms(mic_pcm)

    sx = sy = sxx = syy = sxy = 0.0
    for index in range(count):
        x = float(reference[index])
        y = float(mic[index])
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
    numerator = count * sxy - sx * sy
    denominator = math.sqrt(max(0.0, count * sxx - sx * sx) * max(0.0, count * syy - sy * sy))
    correlation = abs(numerator / denominator) if denominator > 1.0 else 0.0
    gain = sxy / sxx if sxx > 1.0 else 0.0

    residual_square = 0.0
    for index in range(count):
        residual = float(mic[index]) - gain * float(reference[index])
        residual_square += residual * residual
    return min(1.0, correlation), gain, math.sqrt(residual_square / count)


@dataclass(frozen=True)
class EchoReferenceFrame:
    timestamp_ns: int
    sequence: int
    pcm: bytes


class EchoReferenceSender:
    """Publish the rendered 48 kHz stereo stream as 16 kHz mono reference frames."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, socket_impl=None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.host = str(cfg.get("reference_host", "127.0.0.1"))
        self.port = int(cfg.get("reference_port", 19094))
        self._socket = socket_impl
        self._rate_state = None
        self._residual = bytearray()
        self._sequence = 0
        self._active_stream: Optional[str] = None
        self._last_reference_ns: Optional[int] = None
        self._sent_frames = 0
        self._sent_bytes = 0
        self._errors = 0
        self._last_error: Optional[str] = None
        self._lock = threading.Lock()

    def _ensure_socket(self):
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self._socket

    def begin(self, stream_id: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._active_stream = str(stream_id)
            self._rate_state = None
            self._residual.clear()
            self._last_reference_ns = None

    def publish_48k_stereo(self, pcm: bytes, timestamp_ns: int) -> None:
        if not self.enabled or not pcm:
            return
        with self._lock:
            try:
                mono = audioop.tomono(pcm, PCM_WIDTH, 0.5, 0.5)
                converted, self._rate_state = audioop.ratecv(
                    mono, PCM_WIDTH, 1, 48_000, PCM_RATE, self._rate_state
                )
                self._residual.extend(converted)
                offset = 0
                while len(self._residual) >= FRAME_BYTES:
                    frame = bytes(self._residual[:FRAME_BYTES])
                    del self._residual[:FRAME_BYTES]
                    frame_timestamp_ns = int(timestamp_ns) + offset * FRAME_MS * 1_000_000
                    self._sequence += 1
                    packet = HEADER.pack(MAGIC, PACKET_PCM, frame_timestamp_ns, self._sequence) + frame
                    self._ensure_socket().sendto(packet, (self.host, self.port))
                    self._sent_frames += 1
                    self._sent_bytes += len(frame)
                    self._last_reference_ns = frame_timestamp_ns
                    offset += 1
            except Exception as exc:  # reference transport must never break playback
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"

    def end(self, stream_id: Optional[str] = None, timestamp_ns: Optional[int] = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            if stream_id and self._active_stream and str(stream_id) != self._active_stream:
                return
            ended_at_ns = max(
                int(timestamp_ns or time.monotonic_ns()),
                int((self._last_reference_ns or 0) + FRAME_MS * 1_000_000),
            )
            try:
                packet = HEADER.pack(MAGIC, PACKET_END, ended_at_ns, self._sequence)
                self._ensure_socket().sendto(packet, (self.host, self.port))
            except Exception as exc:
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            self._active_stream = None
            self._rate_state = None
            self._residual.clear()

    def close(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "target": f"udp://{self.host}:{self.port}",
            "active": self._active_stream is not None,
            "active_stream": self._active_stream,
            "sent_frames": self._sent_frames,
            "sent_bytes": self._sent_bytes,
            "errors": self._errors,
            "last_error": self._last_error,
        }


class EchoSuppressor:
    """Attenuate playback-dominated microphone frames while preserving double-talk."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.host = str(cfg.get("reference_host", "127.0.0.1"))
        self.port = int(cfg.get("reference_port", 19094))
        self.delay_ms = max(0, int(cfg.get("reference_delay_ms", 250)))
        self.search_ms = max(FRAME_MS, int(cfg.get("reference_search_ms", 80)))
        self.reference_window_ms = max(500, int(cfg.get("reference_window_ms", 1500)))
        self.correlation_threshold = min(0.99, max(0.05, float(cfg.get("correlation_threshold", 0.65))))
        self.attenuation_db = max(0.0, float(cfg.get("echo_attenuation_db", 15.0)))
        self.attenuation = _db_ratio(-self.attenuation_db)
        self.near_end_margin_db = max(0.0, float(cfg.get("near_end_margin_db", 6.0)))
        self.near_end_ratio = _db_ratio(self.near_end_margin_db)
        self.near_end_frames = max(1, int(cfg.get("near_end_frames", 3)))
        self.near_end_hold_ms = max(FRAME_MS, int(cfg.get("near_end_hold_ms", 500)))
        self.near_end_hold_frames = max(1, self.near_end_hold_ms // FRAME_MS)
        self.near_end_min_rms = max(1.0, float(cfg.get("near_end_min_rms", 400)))
        self.tail_ms = max(0, int(cfg.get("tail_ms", 250)))
        self.echo_gain = min(2.0, max(0.001, float(cfg.get("initial_echo_gain", 0.12))))

        capacity = max(32, self.reference_window_ms // FRAME_MS + 32)
        self._references: Deque[EchoReferenceFrame] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._socket = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._listening = False
        self._last_error: Optional[str] = None
        self._playback_end_ns: Optional[int] = None
        self._last_reference_ns: Optional[int] = None
        self._near_end_streak = 0
        self._near_end_hold = 0
        self._metrics = {
            "received_reference_frames": 0,
            "invalid_reference_packets": 0,
            "processed_frames": 0,
            "suppressed_frames": 0,
            "near_end_frames": 0,
            "playback_frames": 0,
            "process_ms_total": 0.0,
            "process_ms_max": 0.0,
        }
        self._last = {
            "state": "idle",
            "mic_rms": 0.0,
            "reference_rms": 0.0,
            "residual_rms": 0.0,
            "correlation": 0.0,
            "echo_gain": self.echo_gain,
        }

    def start(self) -> bool:
        if not self.enabled or self._thread is not None:
            return self.enabled
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.settimeout(0.25)
            self._socket = sock
            self._stop.clear()
            self._thread = threading.Thread(target=self._listen, name="m1-echo-reference", daemon=True)
            self._thread.start()
            self._listening = True
            return True
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._listening = False
            return False

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        self._listening = False

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                packet, _address = self._socket.recvfrom(HEADER.size + FRAME_BYTES + 32)
            except socket.timeout:
                continue
            except OSError:
                return
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                continue
            self.ingest_packet(packet)

    def ingest_packet(self, packet: bytes) -> bool:
        if len(packet) < HEADER.size:
            self._metrics["invalid_reference_packets"] += 1
            return False
        try:
            magic, packet_type, timestamp_ns, sequence = HEADER.unpack_from(packet)
        except struct.error:
            self._metrics["invalid_reference_packets"] += 1
            return False
        if magic != MAGIC:
            self._metrics["invalid_reference_packets"] += 1
            return False
        if packet_type == PACKET_END:
            with self._lock:
                self._playback_end_ns = int(timestamp_ns)
            return True
        if packet_type != PACKET_PCM or len(packet) != HEADER.size + FRAME_BYTES:
            self._metrics["invalid_reference_packets"] += 1
            return False
        self.ingest_reference(packet[HEADER.size:], int(timestamp_ns), int(sequence))
        return True

    def ingest_reference(self, pcm: bytes, timestamp_ns: int, sequence: int = 0) -> None:
        if len(pcm) != FRAME_BYTES:
            raise ValueError(f"echo reference frame must contain {FRAME_BYTES} bytes")
        with self._lock:
            self._references.append(EchoReferenceFrame(int(timestamp_ns), int(sequence), bytes(pcm)))
            self._last_reference_ns = int(timestamp_ns)
            self._playback_end_ns = None
            self._metrics["received_reference_frames"] += 1

    def process(self, pcm: bytes, captured_at_ns: int) -> bytes:
        if not self.enabled or len(pcm) != FRAME_BYTES:
            return pcm
        started = time.perf_counter()
        mic_rms = _pcm_rms(pcm)
        references, playback_active = self._candidate_references(int(captured_at_ns))
        correlation = reference_rms = residual_rms = 0.0
        selected_gain = 0.0
        if references:
            best = None
            for reference in references:
                score = _correlation_and_residual(pcm, reference.pcm)
                if best is None or score[0] > best[0]:
                    best = (*score, reference)
            correlation, selected_gain, residual_rms, selected = best
            reference_rms = _pcm_rms(selected.pcm)

        state = "idle"
        result = pcm
        if playback_active:
            self._metrics["playback_frames"] += 1
            predicted_echo = max(1.0, reference_rms * self.echo_gain)
            if correlation >= self.correlation_threshold and reference_rms > 1.0:
                observed_gain = min(2.0, max(0.001, abs(selected_gain)))
                # Only learn slowly from echo-dominated frames.  A user voice
                # mixed into the microphone must not inflate the echo model.
                if residual_rms < max(self.near_end_min_rms, predicted_echo * self.near_end_ratio):
                    self.echo_gain = self.echo_gain * 0.9 + observed_gain * 0.1
                    predicted_echo = max(1.0, reference_rms * self.echo_gain)

            if self._near_end_hold > 0:
                near_end = True
                self._near_end_hold -= 1
            else:
                evidence_rms = residual_rms if correlation >= self.correlation_threshold else mic_rms
                threshold = max(self.near_end_min_rms, predicted_echo * self.near_end_ratio)
                near_candidate = evidence_rms >= threshold
                self._near_end_streak = self._near_end_streak + 1 if near_candidate else 0
                near_end = self._near_end_streak >= self.near_end_frames
                if near_end:
                    self._near_end_hold = self.near_end_hold_frames

            if near_end:
                state = "near_end"
                self._metrics["near_end_frames"] += 1
                result = pcm
            else:
                state = "suppressed"
                self._metrics["suppressed_frames"] += 1
                result = audioop.mul(pcm, PCM_WIDTH, self.attenuation)
        else:
            self._near_end_streak = 0
            self._near_end_hold = 0

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._metrics["processed_frames"] += 1
        self._metrics["process_ms_total"] += elapsed_ms
        self._metrics["process_ms_max"] = max(self._metrics["process_ms_max"], elapsed_ms)
        self._last = {
            "state": state,
            "mic_rms": round(mic_rms, 1),
            "reference_rms": round(reference_rms, 1),
            "residual_rms": round(residual_rms, 1),
            "correlation": round(correlation, 4),
            "echo_gain": round(self.echo_gain, 4),
        }
        return result

    def _candidate_references(self, captured_at_ns: int) -> Tuple[Iterable[EchoReferenceFrame], bool]:
        delay_ns = self.delay_ms * 1_000_000
        target_ns = captured_at_ns - delay_ns
        search_ns = self.search_ms * 1_000_000
        keep_after_ns = target_ns - self.reference_window_ms * 1_000_000
        with self._lock:
            while self._references and self._references[0].timestamp_ns < keep_after_ns:
                self._references.popleft()
            candidates = tuple(
                frame for frame in self._references
                if abs(frame.timestamp_ns - target_ns) <= search_ns
            )
            last_reference_ns = self._last_reference_ns
            playback_end_ns = self._playback_end_ns

        end_ns = playback_end_ns
        if end_ns is None and last_reference_ns is not None:
            end_ns = last_reference_ns + FRAME_MS * 1_000_000
        active_until_ns = (end_ns + delay_ns + self.tail_ms * 1_000_000) if end_ns else 0
        playback_active = bool(candidates or captured_at_ns <= active_until_ns)
        return candidates, playback_active

    def status(self) -> Dict[str, Any]:
        metrics = dict(self._metrics)
        processed = metrics["processed_frames"]
        metrics["process_ms_average"] = round(
            metrics["process_ms_total"] / processed, 4
        ) if processed else 0.0
        metrics["process_ms_total"] = round(metrics["process_ms_total"], 3)
        metrics["process_ms_max"] = round(metrics["process_ms_max"], 3)
        return {
            "enabled": self.enabled,
            "listening": self._listening,
            "reference": f"udp://{self.host}:{self.port}",
            "reference_delay_ms": self.delay_ms,
            "reference_search_ms": self.search_ms,
            "correlation_threshold": self.correlation_threshold,
            "echo_attenuation_db": self.attenuation_db,
            "near_end_margin_db": self.near_end_margin_db,
            "near_end_frames_required": self.near_end_frames,
            "tail_ms": self.tail_ms,
            "last": dict(self._last),
            "metrics": metrics,
            "last_error": self._last_error,
        }
