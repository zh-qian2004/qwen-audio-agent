"""Realtime speaker enrollment and identification for the microphone Audio Bus.

The service owns one embedding model instance and one inference worker. Audio
ingest never performs model work inline, so a slow speaker model cannot delay
the realtime microphone WebSocket.
"""
from __future__ import annotations

import asyncio
import audioop
import json
import logging
import math
import os
import statistics
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

import numpy as np

from input.audio_frame import AudioFrame

LOG = logging.getLogger(__name__)


def _normalise(values: Iterable[float]) -> List[float]:
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        raise ValueError("speaker embedding has zero norm")
    return [value / norm for value in vector]


def _cosine(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _centroid(embeddings: Iterable[Iterable[float]]) -> List[float]:
    vectors = [list(vector) for vector in embeddings]
    if not vectors:
        raise ValueError("at least one speaker embedding is required")
    return _normalise(statistics.fmean(values) for values in zip(*vectors))


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 3)


class SherpaOnnxSpeakerProvider:
    """CAM++ embedding provider backed by sherpa-onnx."""

    name = "sherpa_onnx_campplus"

    def __init__(self, model_path: str, num_threads: int = 2):
        self.model_path = str(model_path)
        self.num_threads = max(1, int(num_threads))
        self.extractor = None
        self.dimension = None

    def load(self) -> Dict[str, Any]:
        started = time.perf_counter()
        if not os.path.isfile(self.model_path):
            raise FileNotFoundError(self.model_path)
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=self.model_path,
            num_threads=self.num_threads,
            debug=False,
            provider="cpu",
        )
        if not config.validate():
            raise RuntimeError(f"invalid speaker model config: {config}")
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.dimension = int(self.extractor.dim)
        return {
            "provider": self.name,
            "dimension": self.dimension,
            "model_path": self.model_path,
            "load_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    def embed(self, pcm: bytes, sample_rate: int = 16000) -> List[float]:
        if self.extractor is None:
            raise RuntimeError("speaker model is not loaded")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size < sample_rate:
            raise ValueError("speaker audio must contain at least 1 second")
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=sample_rate, waveform=samples)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            raise RuntimeError("speaker model did not accept the audio segment")
        return _normalise(self.extractor.compute(stream))


@dataclass
class Enrollment:
    speaker_id: str
    display_name: str
    role: str
    started_at: float = field(default_factory=time.time)
    pcm: bytearray = field(default_factory=bytearray)
    voiced_ms: int = 0
    dropped_playback_ms: int = 0


class SpeakerIdentityService:
    VERSION = "1.2.0"

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        root: Optional[Path] = None,
        provider: Optional[Any] = None,
        acoustic_state_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        cfg = dict(config or {})
        self.root = Path(root or Path(__file__).resolve().parent.parent)
        self.enabled = bool(cfg.get("enabled", False))
        self.sample_rate = int(cfg.get("sample_rate", 16000))
        self.frame_ms = int(cfg.get("frame_ms", 20))
        self.energy_threshold_dbfs = float(cfg.get("energy_threshold_dbfs", -42.0))
        self.adaptive_energy_enabled = bool(cfg.get("adaptive_energy_enabled", True))
        self.energy_floor_dbfs = float(cfg.get("energy_floor_dbfs", -55.0))
        self.energy_ceiling_dbfs = float(cfg.get("energy_ceiling_dbfs", -35.0))
        self.vad_snr_db = max(1.0, float(cfg.get("vad_snr_db", 6.0)))
        self.start_frames = max(1, int(cfg.get("start_frames", 3)))
        self.end_silence_ms = max(self.frame_ms, int(cfg.get("end_silence_ms", 500)))
        self.min_audio_ms = max(1000, int(cfg.get("min_audio_ms", 1500)))
        self.max_audio_ms = max(self.min_audio_ms, int(cfg.get("max_audio_ms", 12000)))
        self.pre_roll_ms = max(0, int(cfg.get("pre_roll_ms", 300)))
        self.threshold = float(cfg.get("threshold", 0.50))
        self.min_margin = float(cfg.get("min_margin", 0.04))
        self.min_snr_db = max(0.0, float(cfg.get("min_snr_db", 8.0)))
        self.max_clip_ratio = min(1.0, max(0.0, float(cfg.get("max_clip_ratio", 0.01))))
        self.evidence_target_ms = max(
            self.min_audio_ms, int(cfg.get("evidence_target_ms", 1800))
        )
        self.evidence_max_gap_ms = max(0, int(cfg.get("evidence_max_gap_ms", 5000)))
        self.enrollment_sample_ms = max(
            self.min_audio_ms, int(cfg.get("enrollment_sample_ms", 2000))
        )
        self.enrollment_min_samples = max(2, int(cfg.get("enrollment_min_samples", 3)))
        self.enrollment_target_ms = max(
            self.enrollment_sample_ms * self.enrollment_min_samples,
            int(cfg.get("enrollment_target_ms", 6000)),
        )
        self.auto_finish_enrollment = bool(cfg.get("auto_finish_enrollment", True))
        self.num_threads = max(1, int(cfg.get("num_threads", 2)))
        self.history_size = max(10, int(cfg.get("history_size", 100)))
        self.event_queue_size = max(10, int(cfg.get("event_queue_size", 100)))

        model_path = Path(str(cfg.get(
            "model_path",
            "models/speaker/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        )))
        if not model_path.is_absolute():
            model_path = self.root / model_path
        self.model_path = model_path

        registry_path = Path(str(cfg.get("registry_path", "data/speakers.json")))
        if not registry_path.is_absolute():
            registry_path = self.root / registry_path
        self.registry_path = registry_path

        self.provider = provider or SherpaOnnxSpeakerProvider(
            str(self.model_path), num_threads=self.num_threads
        )
        self.acoustic_state_provider = acoustic_state_provider
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speaker-identity")
        self.ready = False
        self.phase = "disabled" if not self.enabled else "created"
        self.last_error: Optional[str] = None
        self.model: Dict[str, Any] = {}
        self.profiles: Dict[str, Dict[str, Any]] = {}
        self.enrollment: Optional[Enrollment] = None

        self.total_frames = 0
        self.voiced_frames = 0
        self.gap_count = 0
        self.last_sequence = None
        self._pre_roll: Deque[bytes] = deque(maxlen=max(1, self.pre_roll_ms // self.frame_ms))
        self._speech_streak = 0
        self._silence_ms = 0
        self._active_pcm: Optional[bytearray] = None
        self._active_voiced_pcm = bytearray()
        self._active_started_at = None
        self._active_start_sequence = None
        self._turn_counter = 0
        self._inference_tasks: set[asyncio.Task] = set()
        self._enrollment_finish_task: Optional[asyncio.Task] = None
        self._active_voiced_dbfs: List[float] = []
        self._active_playback_frames = 0
        self._active_near_end_frames = 0
        self._noise_floor_dbfs = min(-55.0, self.energy_threshold_dbfs - 12.0)
        self._evidence_pcm = bytearray()
        self._evidence_updated_at = 0.0
        self._evidence_turns = 0

        self._events: Deque[Dict[str, Any]] = deque(maxlen=self.history_size)
        self._subscribers: set = set()
        self._inference_ms: Deque[float] = deque(maxlen=200)
        self._queue_wait_ms: Deque[float] = deque(maxlen=200)
        self._identity_ready_ms: Deque[float] = deque(maxlen=200)
        self._ingest_us: Deque[float] = deque(maxlen=1000)
        self._inference_count = 0
        self._filter_counts: Counter = Counter()

    async def start(self) -> bool:
        if not self.enabled:
            self.phase = "disabled"
            return False
        if self.ready:
            return True
        self.phase = "loading"
        try:
            loop = asyncio.get_running_loop()
            self.model = await loop.run_in_executor(self.executor, self.provider.load)
            self._load_profiles()
            self.ready = True
            self.phase = "ready"
            self.last_error = None
            LOG.info(
                "说话人模型就绪 provider=%s dimension=%s load_ms=%s profiles=%d",
                self.model.get("provider"), self.model.get("dimension"),
                self.model.get("load_ms"), len(self.profiles),
            )
            return True
        except Exception as exc:
            self.ready = False
            self.phase = "failed"
            self.last_error = f"{type(exc).__name__}: {exc}"
            LOG.exception("说话人模型加载失败")
            return False

    async def stop(self) -> None:
        if self._enrollment_finish_task is not None:
            self._enrollment_finish_task.cancel()
            await asyncio.gather(self._enrollment_finish_task, return_exceptions=True)
            self._enrollment_finish_task = None
        tasks = list(self._inference_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inference_tasks.clear()
        self.ready = False
        if self.phase != "failed":
            self.phase = "stopped"
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _load_profiles(self) -> None:
        self.profiles = {}
        if not self.registry_path.exists():
            return
        with self.registry_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        for item in payload.get("items", []):
            speaker_id = str(item.get("speaker_id", "")).strip()
            embedding = item.get("embedding")
            if not speaker_id or not isinstance(embedding, list):
                continue
            self.profiles[speaker_id] = {
                "speaker_id": speaker_id,
                "display_name": str(item.get("display_name") or speaker_id),
                "role": str(item.get("role") or "guest"),
                "embedding": _normalise(embedding),
                "created_at": float(item.get("created_at") or time.time()),
                "updated_at": float(item.get("updated_at") or time.time()),
                "sample_count": int(item.get("sample_count") or 1),
                "voiced_ms": int(item.get("voiced_ms") or 0),
                "sample_dispersion": item.get("sample_dispersion"),
            }

    def _save_profiles(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "items": list(self.profiles.values()),
        }
        temporary = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.chmod(temporary, 0o600)
        temporary.replace(self.registry_path)

    def list_profiles(self) -> List[Dict[str, Any]]:
        return [
            {key: value for key, value in profile.items() if key != "embedding"}
            for profile in sorted(self.profiles.values(), key=lambda item: item["speaker_id"])
        ]

    async def begin_enrollment(self, speaker_id: str, display_name: str, role: str) -> Dict[str, Any]:
        if not self.ready:
            raise RuntimeError("speaker model is not ready")
        speaker_id = str(speaker_id or "").strip()
        if not speaker_id or len(speaker_id) > 80:
            raise ValueError("speaker_id is required and must be at most 80 characters")
        if self.enrollment is not None:
            raise RuntimeError("another enrollment is active")
        self.enrollment = Enrollment(
            speaker_id=speaker_id,
            display_name=str(display_name or speaker_id).strip()[:120],
            role=str(role or "guest").strip()[:40],
        )
        return self.enrollment_status()

    async def finish_enrollment(self) -> Dict[str, Any]:
        enrollment = self.enrollment
        if enrollment is None:
            raise RuntimeError("no enrollment is active")
        if enrollment.voiced_ms < self.enrollment_target_ms:
            raise ValueError(
                f"not enough voiced audio: {enrollment.voiced_ms}ms, "
                f"need {self.enrollment_target_ms}ms across "
                f"{self.enrollment_min_samples} samples"
            )
        sample_bytes = self.enrollment_sample_ms * self.sample_rate * 2 // 1000
        samples = [
            bytes(enrollment.pcm[offset:offset + sample_bytes])
            for offset in range(0, len(enrollment.pcm), sample_bytes)
            if len(enrollment.pcm[offset:offset + sample_bytes]) >= self.min_audio_ms * self.sample_rate * 2 // 1000
        ][:self.enrollment_min_samples]
        if len(samples) < self.enrollment_min_samples:
            raise ValueError(
                f"not enough enrollment samples: {len(samples)}, "
                f"need {self.enrollment_min_samples}"
            )
        embedded = [await self._embed(sample) for sample in samples]
        if self.enrollment is not enrollment:
            raise RuntimeError("speaker enrollment was cancelled during finalization")
        embeddings = [item[0] for item in embedded]
        embedding = _centroid(embeddings)
        similarities = [_cosine(vector, embedding) for vector in embeddings]
        timings = {
            "inference_ms": round(sum(item[1]["inference_ms"] for item in embedded), 3),
            "queue_wait_ms": round(sum(item[1]["queue_wait_ms"] for item in embedded), 3),
        }
        if self.enrollment is enrollment:
            self.enrollment = None
        now = time.time()
        previous = self.profiles.get(enrollment.speaker_id)
        self.profiles[enrollment.speaker_id] = {
            "speaker_id": enrollment.speaker_id,
            "display_name": enrollment.display_name,
            "role": enrollment.role,
            "embedding": embedding,
            "created_at": previous.get("created_at", now) if previous else now,
            "updated_at": now,
            "sample_count": len(samples),
            "voiced_ms": enrollment.voiced_ms,
            "sample_dispersion": round(1.0 - min(similarities), 4),
        }
        self._save_profiles()
        result = {
            "speaker_id": enrollment.speaker_id,
            "display_name": enrollment.display_name,
            "role": enrollment.role,
            "voiced_ms": enrollment.voiced_ms,
            "sample_count": len(samples),
            "sample_dispersion": round(1.0 - min(similarities), 4),
            "auto_completed": enrollment.voiced_ms >= self.enrollment_target_ms,
            **timings,
        }
        await self._publish({"type": "speaker.enrolled", **result})
        return result

    def cancel_enrollment(self) -> Dict[str, Any]:
        active = self.enrollment
        self.enrollment = None
        task = self._enrollment_finish_task
        if task is not None and not task.done():
            task.cancel()
        return {
            "cancelled": active is not None,
            "speaker_id": active.speaker_id if active else None,
        }

    def delete_profile(self, speaker_id: str) -> bool:
        removed = self.profiles.pop(str(speaker_id), None)
        if removed is not None:
            self._save_profiles()
        return removed is not None

    def enrollment_status(self) -> Dict[str, Any]:
        if self.enrollment is None:
            return {"active": False}
        return {
            "active": True,
            "speaker_id": self.enrollment.speaker_id,
            "display_name": self.enrollment.display_name,
            "role": self.enrollment.role,
            "voiced_ms": self.enrollment.voiced_ms,
            "target_ms": self.enrollment_target_ms,
            "sample_ms": self.enrollment_sample_ms,
            "required_samples": self.enrollment_min_samples,
            "collected_samples": min(
                self.enrollment_min_samples,
                self.enrollment.voiced_ms // self.enrollment_sample_ms,
            ),
            "dropped_playback_ms": self.enrollment.dropped_playback_ms,
            "auto_finish_pending": self._enrollment_finish_task is not None,
            "started_at": round(self.enrollment.started_at, 3),
        }

    async def accept(self, frame: AudioFrame) -> None:
        started = time.perf_counter()
        self.total_frames += 1
        if self.last_sequence is not None and frame.sequence != self.last_sequence + 1:
            self.gap_count += 1
        self.last_sequence = frame.sequence

        acoustic_state = self._acoustic_state()
        rms = audioop.rms(frame.pcm, 2) if frame.pcm else 0
        dbfs = 20.0 * math.log10(rms / 32768.0) if rms else -96.0
        voice_threshold_dbfs = self._voice_threshold_dbfs()
        voiced = dbfs >= voice_threshold_dbfs
        usable_voice = voiced and acoustic_state != "suppressed"
        if not voiced and acoustic_state == "idle":
            self._noise_floor_dbfs = self._noise_floor_dbfs * 0.98 + dbfs * 0.02
        if usable_voice:
            self.voiced_frames += 1
            self._speech_streak += 1
            self._silence_ms = 0
            if self.enrollment is not None:
                if acoustic_state == "idle" and self.enrollment.voiced_ms < self.enrollment_target_ms:
                    self.enrollment.pcm.extend(frame.pcm)
                    self.enrollment.voiced_ms += self.frame_ms
                    self._maybe_auto_finish_enrollment(self.enrollment)
                elif acoustic_state != "idle":
                    self.enrollment.dropped_playback_ms += self.frame_ms
                    self._filter_counts["enrollment_playback"] += 1
        else:
            self._speech_streak = 0
            if self._active_pcm is not None:
                self._silence_ms += self.frame_ms

        if self._active_pcm is None:
            self._pre_roll.append(frame.pcm)
            if self._speech_streak >= self.start_frames:
                self._turn_counter += 1
                self._active_pcm = bytearray(b"".join(self._pre_roll))
                self._active_started_at = time.monotonic()
                self._active_start_sequence = frame.sequence - len(self._pre_roll) + 1
                self._silence_ms = 0
                self._active_voiced_pcm = bytearray(frame.pcm if usable_voice else b"")
                self._active_voiced_dbfs = [dbfs] if usable_voice else []
                self._active_playback_frames = 0
                self._active_near_end_frames = 0
                await self._publish({
                    "type": "speaker.turn.started",
                    "speaker_turn_id": f"speaker-{self._turn_counter}",
                    "audio_start_sequence": self._active_start_sequence,
                })
        else:
            self._active_pcm.extend(frame.pcm)
            if usable_voice:
                self._active_voiced_dbfs.append(dbfs)
                self._active_voiced_pcm.extend(frame.pcm)
            if acoustic_state in {"suppressed", "near_end"}:
                self._active_playback_frames += 1
            if acoustic_state == "near_end":
                self._active_near_end_frames += 1
            active_ms = len(self._active_pcm) * 1000 // (self.sample_rate * 2)
            if self._silence_ms >= self.end_silence_ms or active_ms >= self.max_audio_ms:
                pcm = bytes(self._active_pcm)
                voiced_pcm = bytes(self._active_voiced_pcm)
                started_at = self._active_started_at or time.monotonic()
                start_sequence = self._active_start_sequence
                turn_id = f"speaker-{self._turn_counter}"
                self._active_pcm = None
                self._active_voiced_pcm.clear()
                self._active_started_at = None
                self._active_start_sequence = None
                self._silence_ms = 0
                self._pre_roll.clear()
                segment_ms = len(pcm) * 1000 // (self.sample_rate * 2)
                audio_ms = len(voiced_pcm) * 1000 // (self.sample_rate * 2)
                voiced_dbfs = self._active_voiced_dbfs
                playback_frames = self._active_playback_frames
                near_end_frames = self._active_near_end_frames
                self._active_voiced_dbfs = []
                self._active_playback_frames = 0
                self._active_near_end_frames = 0
                await self._complete_segment(
                    pcm=voiced_pcm,
                    turn_id=turn_id,
                    started_at=started_at,
                    start_sequence=start_sequence,
                    end_sequence=frame.sequence,
                    audio_ms=audio_ms,
                    voiced_dbfs=voiced_dbfs,
                    playback_frames=playback_frames,
                    near_end_frames=near_end_frames,
                    segment_ms=segment_ms,
                )

        self._ingest_us.append((time.perf_counter() - started) * 1_000_000.0)

    def _acoustic_state(self) -> str:
        if self.acoustic_state_provider is None:
            return "idle"
        try:
            status = self.acoustic_state_provider() or {}
            return str((status.get("last") or {}).get("state") or "idle")
        except Exception:
            return "idle"

    def _voice_threshold_dbfs(self) -> float:
        if not self.adaptive_energy_enabled:
            return self.energy_threshold_dbfs
        lower = min(self.energy_floor_dbfs, self.energy_ceiling_dbfs)
        upper = max(self.energy_floor_dbfs, self.energy_ceiling_dbfs)
        return min(upper, max(lower, self._noise_floor_dbfs + self.vad_snr_db))

    def _maybe_auto_finish_enrollment(self, enrollment: Enrollment) -> None:
        if not self.auto_finish_enrollment:
            return
        if enrollment.voiced_ms < self.enrollment_target_ms:
            return
        if self._enrollment_finish_task is not None:
            return

        async def finish() -> None:
            try:
                if self.enrollment is enrollment:
                    await self.finish_enrollment()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOG.exception("自动完成说话人注册失败 speaker=%s", enrollment.speaker_id)
                await self._publish({
                    "type": "speaker.enrollment.failed",
                    "speaker_id": enrollment.speaker_id,
                    "error": self.last_error,
                })
            finally:
                self._enrollment_finish_task = None

        self._enrollment_finish_task = asyncio.create_task(
            finish(), name=f"speaker-enroll-{enrollment.speaker_id}"
        )

    async def _complete_segment(
        self,
        *,
        pcm: bytes,
        turn_id: str,
        started_at: float,
        start_sequence: Optional[int],
        end_sequence: int,
        audio_ms: int,
        voiced_dbfs: List[float],
        playback_frames: int,
        near_end_frames: int,
        segment_ms: int,
    ) -> None:
        common = {
            "speaker_turn_id": turn_id,
            "speaker_id": None,
            "confidence": None,
            "audio_ms": audio_ms,
            "audio_start_sequence": start_sequence,
            "audio_end_sequence": end_sequence,
        }
        if not self.profiles:
            self._clear_evidence()
            self._filter_counts["no_profiles"] += 1
            await self._publish({"type": "speaker.identity", "status": "no_profiles", **common})
            return

        reason, quality = self._segment_quality(
            pcm, voiced_dbfs, playback_frames, near_end_frames
        )
        if reason is not None:
            self._clear_evidence()
            self._filter_counts[reason] += 1
            await self._publish({
                "type": "speaker.identity",
                "status": "unknown",
                "reason": reason,
                "quality": quality,
                "segment_ms": segment_ms,
                **common,
            })
            return

        now = time.monotonic()
        evidence_pcm = bytes(pcm)
        evidence_turns = 1
        if (
            self._evidence_pcm
            and (now - self._evidence_updated_at) * 1000.0 <= self.evidence_max_gap_ms
            and audio_ms < self.evidence_target_ms
        ):
            evidence_pcm = bytes(self._evidence_pcm) + evidence_pcm
            evidence_turns += self._evidence_turns

        evidence_ms = len(evidence_pcm) * 1000 // (self.sample_rate * 2)
        if evidence_ms < self.min_audio_ms:
            self._evidence_pcm = bytearray(evidence_pcm)
            self._evidence_updated_at = now
            self._evidence_turns = evidence_turns
            self._filter_counts["too_short"] += 1
            await self._publish({
                "type": "speaker.identity",
                "status": "insufficient_audio",
                "reason": "too_short",
                "evidence_audio_ms": evidence_ms,
                "evidence_turns": evidence_turns,
                "quality": quality,
                "segment_ms": segment_ms,
                **common,
            })
            return

        self._evidence_pcm.clear()
        self._evidence_updated_at = 0.0
        self._evidence_turns = 0
        self._schedule_identification(
            evidence_pcm,
            turn_id,
            started_at,
            start_sequence,
            end_sequence,
            audio_ms,
            evidence_audio_ms=evidence_ms,
            evidence_turns=evidence_turns,
            quality=quality,
        )

    def _clear_evidence(self) -> None:
        self._evidence_pcm.clear()
        self._evidence_updated_at = 0.0
        self._evidence_turns = 0

    def _segment_quality(
        self,
        pcm: bytes,
        voiced_dbfs: List[float],
        playback_frames: int,
        near_end_frames: int,
    ) -> tuple[Optional[str], Dict[str, Any]]:
        samples = np.frombuffer(pcm, dtype="<i2") if pcm else np.array([], dtype=np.int16)
        clip_ratio = float(np.mean(np.abs(samples.astype(np.int32)) >= 32700)) if samples.size else 0.0
        speech_dbfs = statistics.median(voiced_dbfs) if voiced_dbfs else -96.0
        snr_db = speech_dbfs - self._noise_floor_dbfs
        quality = {
            "snr_db": round(snr_db, 2),
            "clip_ratio": round(clip_ratio, 5),
            "playback_frames": playback_frames,
            "near_end_frames": near_end_frames,
        }
        if playback_frames and near_end_frames == 0:
            return "playback_active", quality
        if clip_ratio > self.max_clip_ratio:
            return "clipped", quality
        if snr_db < self.min_snr_db:
            return "low_snr", quality
        return None, quality

    def _schedule_identification(
        self,
        pcm: bytes,
        turn_id: str,
        started_at: float,
        start_sequence: Optional[int],
        end_sequence: int,
        audio_ms: int,
        *,
        evidence_audio_ms: Optional[int] = None,
        evidence_turns: int = 1,
        quality: Optional[Dict[str, Any]] = None,
    ) -> None:
        queued_at = time.monotonic()

        async def run():
            try:
                embedding, timings = await self._embed(pcm, queued_at=queued_at)
                ranked = sorted(
                    (
                        (_cosine(embedding, profile["embedding"]), profile)
                        for profile in self.profiles.values()
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                best_score, best = ranked[0]
                second_score = ranked[1][0] if len(ranked) > 1 else -1.0
                margin = best_score - second_score
                profile_threshold = float(best.get("threshold", self.threshold))
                verified = best_score >= profile_threshold and margin >= self.min_margin
                decision_reason = None
                if best_score < profile_threshold:
                    decision_reason = "low_score"
                elif margin < self.min_margin:
                    decision_reason = "low_margin"
                self._filter_counts["verified" if verified else decision_reason] += 1
                identity_ready_ms = (time.monotonic() - started_at) * 1000.0
                self._identity_ready_ms.append(identity_ready_ms)
                payload = {
                    "type": "speaker.identity",
                    "speaker_turn_id": turn_id,
                    "speaker_id": best["speaker_id"] if verified else None,
                    "display_name": best["display_name"] if verified else None,
                    "role": best["role"] if verified else None,
                    "status": "verified" if verified else "unknown",
                    "reason": decision_reason,
                    "confidence": round(best_score, 4),
                    "margin": round(margin, 4),
                    "threshold": round(profile_threshold, 4),
                    "audio_ms": audio_ms,
                    "evidence_audio_ms": evidence_audio_ms or audio_ms,
                    "evidence_turns": evidence_turns,
                    "quality": quality or {},
                    "audio_start_sequence": start_sequence,
                    "audio_end_sequence": end_sequence,
                    "identity_ready_ms": round(identity_ready_ms, 3),
                    **timings,
                }
                await self._publish(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOG.exception("说话人识别失败 turn=%s", turn_id)
                await self._publish({
                    "type": "speaker.identity",
                    "speaker_turn_id": turn_id,
                    "speaker_id": None,
                    "status": "failed",
                    "error": self.last_error,
                })

        task = asyncio.create_task(run(), name=f"speaker-identify-{turn_id}")
        self._inference_tasks.add(task)
        task.add_done_callback(self._inference_tasks.discard)

    async def _embed(
        self, pcm: bytes, queued_at: Optional[float] = None
    ) -> tuple[List[float], Dict[str, Any]]:
        queued_at = queued_at or time.monotonic()
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        queue_wait_ms = (started - queued_at) * 1000.0
        embedding = await loop.run_in_executor(
            self.executor, self.provider.embed, pcm, self.sample_rate
        )
        inference_ms = (time.monotonic() - started) * 1000.0
        self._queue_wait_ms.append(queue_wait_ms)
        self._inference_ms.append(inference_ms)
        self._inference_count += 1
        return embedding, {
            "queue_wait_ms": round(queue_wait_ms, 3),
            "inference_ms": round(inference_ms, 3),
        }

    async def benchmark(self, pcm: bytes, iterations: int = 5) -> Dict[str, Any]:
        if not self.ready:
            raise RuntimeError("speaker model is not ready")
        if len(pcm) < self.sample_rate * 2:
            raise ValueError("benchmark requires at least one second of PCM")
        samples = []
        for _ in range(max(1, min(int(iterations), 50))):
            _embedding, timing = await self._embed(pcm)
            samples.append(timing["inference_ms"])
        audio_ms = len(pcm) * 1000.0 / (self.sample_rate * 2)
        return {
            "iterations": len(samples),
            "audio_ms": round(audio_ms, 3),
            "inference_ms": {
                "min": round(min(samples), 3),
                "mean": round(statistics.fmean(samples), 3),
                "p50": _percentile(samples, 0.50),
                "p95": _percentile(samples, 0.95),
                "max": round(max(samples), 3),
            },
            "realtime_factor_mean": round(statistics.fmean(samples) / audio_ms, 5),
        }

    async def _publish(self, payload: Dict[str, Any]) -> None:
        event = {**payload, "occurred_at": round(time.time(), 3)}
        self._events.append(event)
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=self.event_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def status(self) -> Dict[str, Any]:
        inference = list(self._inference_ms)
        queue_wait = list(self._queue_wait_ms)
        identity_ready = list(self._identity_ready_ms)
        ingest = list(self._ingest_us)
        return {
            "version": self.VERSION,
            "enabled": self.enabled,
            "ready": self.ready,
            "phase": self.phase,
            "provider": getattr(self.provider, "name", type(self.provider).__name__),
            "model": self.model,
            "profile_count": len(self.profiles),
            "enrollment": self.enrollment_status(),
            "input": {
                "sample_rate": self.sample_rate,
                "frame_ms": self.frame_ms,
                "total_frames": self.total_frames,
                "voiced_frames": self.voiced_frames,
                "gap_count": self.gap_count,
            },
            "latency": {
                "inference_count": self._inference_count,
                "inference_ms_p50": _percentile(inference, 0.50),
                "inference_ms_p95": _percentile(inference, 0.95),
                "queue_wait_ms_p95": _percentile(queue_wait, 0.95),
                "identity_ready_ms_p95": _percentile(identity_ready, 0.95),
                "ingest_us_p95": _percentile(ingest, 0.95),
            },
            "decisions": dict(self._filter_counts),
            "evidence": {
                "buffered_audio_ms": len(self._evidence_pcm) * 1000 // (self.sample_rate * 2),
                "buffered_turns": self._evidence_turns,
                "target_ms": self.evidence_target_ms,
                "max_gap_ms": self.evidence_max_gap_ms,
            },
            "thresholds": {
                "energy_dbfs": self.energy_threshold_dbfs,
                "adaptive_energy_enabled": self.adaptive_energy_enabled,
                "current_energy_dbfs": round(self._voice_threshold_dbfs(), 2),
                "energy_floor_dbfs": self.energy_floor_dbfs,
                "energy_ceiling_dbfs": self.energy_ceiling_dbfs,
                "vad_snr_db": self.vad_snr_db,
                "speaker": self.threshold,
                "min_margin": self.min_margin,
                "min_audio_ms": self.min_audio_ms,
                "min_snr_db": self.min_snr_db,
                "max_clip_ratio": self.max_clip_ratio,
                "noise_floor_dbfs": round(self._noise_floor_dbfs, 2),
                "enrollment_target_ms": self.enrollment_target_ms,
                "enrollment_min_samples": self.enrollment_min_samples,
                "auto_finish_enrollment": self.auto_finish_enrollment,
            },
            "last_error": self.last_error,
            "recent_events": list(self._events)[-10:],
        }
