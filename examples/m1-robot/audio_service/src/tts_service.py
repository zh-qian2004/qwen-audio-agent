"""TTS 服务编排：合成 → 解码 → ROS 发布 → 播放等待。

同一时刻只有一个任务向 /audio/speaker_pcm 发布 PCM（串行队列 + 单 worker）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from audio_converter import AudioConverter
from dashscope_provider import DashScopeProvider
from queue_manager import VALID_PRIORITIES, PlaybackQueue
from playback_arbiter import (
    CombinedCancel,
    PRIORITY_TTS_HIGH,
    PRIORITY_TTS_NORMAL,
    PlaybackArbiter,
)
from ros_pcm_publisher import RosPcmPublisher
from state import IdempotencyCache, ServiceError, TaskHistory, TtsTask, redact_secret

LOG = logging.getLogger(__name__)

PHASE_LABELS = {
    "queued": "排队中",
    "synthesizing": "合成中",
    "decoding": "解码中",
    "publishing": "发布中",
    "waiting_playback": "等待播放完成",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


class TtsService:
    VERSION = "1.4.0"

    def __init__(
        self,
        config: Dict[str, Any],
        provider: Optional[Any] = None,
        converter: Optional[Any] = None,
        publisher: Optional[Any] = None,
        runtime: Optional[Any] = None,
        capture_stats: Optional[Any] = None,
        arbiter: Optional[PlaybackArbiter] = None,
        manage_publisher: bool = True,
    ):
        self.config = config or {}
        tts_cfg = self.config.get("tts", {})
        queue_cfg = self.config.get("queue", {})
        audio_cfg = self.config.get("audio", {})
        ros_cfg = self.config.get("ros", {})
        media_cfg = self.config.get("media_audio", {})
        history_cfg = self.config.get("history", {})

        self.max_text_length = int(tts_cfg.get("max_text_length", 300))
        self.synthesis_timeout = float(tts_cfg.get("synthesis_timeout_seconds", 30))
        self.decode_timeout = float(tts_cfg.get("decode_timeout_seconds", 30))
        self.queue_capacity = int(queue_cfg.get("capacity", 8))
        self.publish_timeout = float(queue_cfg.get("publish_timeout_seconds", 180))
        self.drain_seconds = float(queue_cfg.get("playback_drain_seconds", 0.4))
        self.chunk_ms = float(audio_cfg.get("chunk_ms", 60))
        self.lead_chunks = int(audio_cfg.get("publish_lead_chunks", 2))

        self.provider = provider if provider is not None else DashScopeProvider(tts_cfg)
        self.converter = converter if converter is not None else AudioConverter(audio_cfg)
        self.publisher = publisher if publisher is not None else RosPcmPublisher(
            ros_cfg,
            media_audio_process=str(media_cfg.get("process_name", "media_audio")),
            runtime=runtime,
            capture_stats=capture_stats,
        )
        self.arbiter = arbiter if arbiter is not None else PlaybackArbiter(self.config.get("playback", {}))
        self.manage_publisher = bool(manage_publisher)
        self.queue = PlaybackQueue(self.queue_capacity)
        self.history = TaskHistory(int(history_cfg.get("max_items", 50)))
        self.idempotency = IdempotencyCache(float(tts_cfg.get("idempotency_ttl_seconds", 600)))

        self.model = str(tts_cfg.get("model", "qwen-audio-3.1-tts-flash"))
        self.voice = str(tts_cfg.get("voice", "longanhuan_v3.1"))
        self.voices = self._normalize_options(
            tts_cfg.get("voices"),
            [{"id": self.voice, "label": self.voice}],
        )
        if self.voice not in {item["id"] for item in self.voices}:
            self.voices.insert(0, {"id": self.voice, "label": self.voice})
        self.default_emotion = str(tts_cfg.get("default_emotion", "neutral"))
        self.emotions = self._normalize_options(
            tts_cfg.get("emotions"),
            [{"id": "neutral", "label": "自然"}],
        )
        if self.default_emotion not in {item["id"] for item in self.emotions}:
            self.emotions.insert(0, {"id": self.default_emotion, "label": self.default_emotion})
        rate_cfg = tts_cfg.get("speech_rate", {})
        self.default_speech_rate = float(rate_cfg.get("default", 1.0))
        self.min_speech_rate = float(rate_cfg.get("min", 0.5))
        self.max_speech_rate = float(rate_cfg.get("max", 2.0))
        self.speech_rate_step = float(rate_cfg.get("step", 0.05))
        self.speaker_topic = str(ros_cfg.get("speaker_topic", "/audio/speaker_pcm"))

        self._tasks: Dict[str, TtsTask] = {}
        self._current: Optional[TtsTask] = None
        self._cancel = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None
        self._started_at: Optional[float] = None
        self._stopping = False
        self._ros_ready = False
        self._recent_errors: List[Dict[str, Any]] = []
        self.counters = {"total": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "deduplicated": 0}

    # ---- 生命周期 -------------------------------------------------
    async def start(self) -> Dict[str, Any]:
        self._started_at = time.time()
        self._stopping = False
        self._ros_ready = await asyncio.to_thread(self.publisher.start)
        if not self._ros_ready:
            LOG.error("ROS 发布器未就绪，speak 请求将返回 ros_unavailable")
        self._worker = asyncio.create_task(self._worker_loop(), name="tts-worker")
        LOG.info(
            "audio_service 启动 version=%s model=%s voice=%s topic=%s queue=%d",
            self.VERSION,
            self.model,
            self.voice,
            self.speaker_topic,
            self.queue_capacity,
        )
        return {"ros_ready": self._ros_ready}

    async def stop(self) -> None:
        self._stopping = True
        self._cancel.set()
        drained = await self.queue.drain()
        for task in drained:
            task.cancel("service stopped")
        await self.queue.close()
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                await asyncio.wait_for(worker, timeout=5.0)
            except asyncio.TimeoutError:
                worker.cancel()
        if self.manage_publisher:
            await asyncio.to_thread(self.publisher.stop)
        LOG.info("audio_service 已停止")

    # ---- 对外 API -------------------------------------------------
    async def speak(
        self,
        text: str,
        client_request_id: Optional[str] = None,
        priority: str = "normal",
        interrupt: bool = False,
        voice: Optional[str] = None,
        emotion: Optional[str] = None,
        speech_rate: Optional[float] = None,
    ) -> Dict[str, Any]:
        text = self._validate_text(text)
        voice = self._validate_choice("voice", voice or self.voice, self.voices)
        emotion = self._validate_choice(
            "emotion", emotion or self.default_emotion, self.emotions
        )
        speech_rate = self._validate_speech_rate(
            self.default_speech_rate if speech_rate is None else speech_rate
        )
        priority = (priority or "normal").strip().lower()
        if priority not in VALID_PRIORITIES:
            raise ServiceError(
                "invalid_priority",
                f"priority 只支持 {list(VALID_PRIORITIES)}",
                400,
                {"priority": priority},
            )

        existing_id = self.idempotency.get(client_request_id) if client_request_id else None
        if existing_id:
            existing = self._tasks.get(existing_id)
            if existing is not None:
                self.counters["deduplicated"] += 1
                position = 0
                if existing.phase == "queued":
                    position = await self._position_of(existing.task_id)
                snapshot = existing.snapshot(queue_position=position if existing.phase == "queued" else None)
                snapshot["deduplicated"] = True
                LOG.info("幂等命中 client_request_id=%s task_id=%s", client_request_id, existing.task_id)
                return {
                    "ok": True,
                    "deduplicated": True,
                    "task_id": existing.task_id,
                    "phase": existing.phase,
                    "queue_position": snapshot["queue_position"],
                    "queue_size": self.queue.pending,
                    "model": self.model,
                    "voice": existing.voice,
                    "emotion": existing.emotion,
                    "speech_rate": existing.speech_rate,
                    "task": snapshot,
                }

        task = TtsTask(
            text=text,
            client_request_id=client_request_id,
            priority=priority,
            interrupt=interrupt,
            voice=voice,
            emotion=emotion,
            speech_rate=speech_rate,
        )
        front = priority == "high" or interrupt
        position = await self.queue.put(task, front=front)
        self._tasks[task.task_id] = task
        self.idempotency.put(client_request_id, task.task_id)
        self.counters["total"] += 1
        LOG.info(
            "新任务入队 task_id=%s priority=%s interrupt=%s voice=%s emotion=%s speech_rate=%.2f position=%d len=%d",
            task.task_id,
            priority,
            interrupt,
            voice,
            emotion,
            speech_rate,
            position,
            len(text),
        )
        return {
            "ok": True,
            "deduplicated": False,
            "task_id": task.task_id,
            "phase": task.phase,
            "queue_position": position,
            "queue_size": self.queue.pending,
            "current_task_id": self._current.task_id if self._current else None,
            "model": self.model,
            "voice": task.voice,
            "emotion": task.emotion,
            "speech_rate": task.speech_rate,
            "task": task.snapshot(queue_position=position),
        }

    async def stop_playback(self) -> Dict[str, Any]:
        current = self._current
        self._cancel.set()
        drained = await self.queue.drain()
        for task in drained:
            if not task.is_terminal:
                task.cancel("cleared by /api/tts/stop")
        remaining = 0.0
        if current is not None and not current.is_terminal:
            remaining = max(0.0, float(current.remaining_playback_seconds or 0.0))
        LOG.info("停止播报：清空 %d 个排队任务，当前任务=%s", len(drained), current.task_id if current else None)
        return {
            "ok": True,
            "cancelled_queued": len(drained),
            "current_task_id": current.task_id if current else None,
            "current_phase": current.phase if current else None,
            "current_phase_label": PHASE_LABELS.get(current.phase) if current else None,
            "estimated_remaining_playback_seconds": round(remaining, 3),
            "pcm_publishing_stopped": current is None or current.phase in ("queued", "synthesizing", "decoding"),
            "playback_control": self.playback_control(),
            "playback": self.arbiter.snapshot(),
        }

    def get_task(self, task_id: str) -> Dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            raise ServiceError("task_not_found", f"未知任务 {task_id}", 404, {"task_id": task_id})
        return task.snapshot()

    def history_payload(self, limit: int = 20) -> Dict[str, Any]:
        return {"items": self.history.list(limit), "max_items": self.history.max_items}

    async def status(self) -> Dict[str, Any]:
        queue = await self.queue.snapshot()
        current = self._current
        return {
            "ok": True,
            "version": self.VERSION,
            "uptime_seconds": round(time.time() - self._started_at, 1) if self._started_at else 0.0,
            "model": self.model,
            "voice": self.voice,
            "controls": self.controls(),
            "provider": self.provider.status(),
            "current_task": current.snapshot() if current else None,
            "queue": queue,
            "queue_size": queue["pending"],
            "queue_capacity": self.queue_capacity,
            "recent_tasks": self.history.list(5),
            "ros": self.publisher.status(),
            "media_audio": self.publisher.media_audio_status(),
            "last_error": self._last_error(),
            "playback": self.arbiter.snapshot(),
            "playback_control": self.playback_control(),
            "stats": dict(self.counters),
        }

    async def health(self) -> Dict[str, Any]:
        queue = await self.queue.snapshot()
        provider = self.provider.status()
        ros = self.publisher.status()
        media = self.publisher.media_audio_status()
        healthy = bool(provider["available"]) and bool(ros["available"]) and bool(media["running"])
        return {
            "status": "ok" if healthy else "degraded",
            "version": self.VERSION,
            "uptime_seconds": round(time.time() - self._started_at, 1) if self._started_at else 0.0,
            "provider": {
                "available": provider["available"],
                "reason": provider["unavailable_reason"],
                "model": provider["model"],
                "voice": provider["voice"],
                "last_error": provider["last_error"],
            },
            "ros": {
                "available": ros["available"],
                "topic": ros["topic"],
                "encoding": ros["encoding"],
                "subscribers": ros["subscribers"],
                "capture_rate_hz": ros["capture"]["rate_hz"],
                "last_error": ros["error"],
            },
            "queue": {"pending": queue["pending"], "capacity": queue["capacity"]},
            "media_audio": media,
            "last_error": self._last_error(),
        }

    # ---- 内部 -----------------------------------------------------
    def playback_control(self) -> Dict[str, Any]:
        return {
            "interrupt_topic": "/audio/speaker_pcm_interrupt",
            "interrupt_topic_type": "std_msgs/msg/String",
            "enabled": False,
            "reason": "厂商插播话题的消息格式与优先级语义尚未确认，interrupt=true 目前退化为同普通任务直接排在队首。",
        }

    def controls(self) -> Dict[str, Any]:
        return {
            "voices": [dict(item) for item in self.voices],
            "emotions": [dict(item) for item in self.emotions],
            "defaults": {
                "voice": self.voice,
                "emotion": self.default_emotion,
                "speech_rate": self.default_speech_rate,
            },
            "speech_rate": {
                "min": self.min_speech_rate,
                "max": self.max_speech_rate,
                "step": self.speech_rate_step,
                "default": self.default_speech_rate,
            },
        }

    def _last_error(self) -> Optional[Dict[str, Any]]:
        return self._recent_errors[-1] if self._recent_errors else None

    def _remember_error(self, task: TtsTask) -> None:
        if not task.error:
            return
        entry = {
            "task_id": task.task_id,
            "code": task.error.get("code"),
            "message": task.error.get("message"),
            "at": round(task.updated_at, 3),
        }
        self._recent_errors.append(entry)
        del self._recent_errors[:-10]

    def _validate_text(self, text: Optional[str]) -> str:
        if text is None:
            raise ServiceError("invalid_text", "text 不能为空", 400)
        value = str(text).strip()
        if not value:
            raise ServiceError("invalid_text", "text 不能为空", 400)
        if len(value) > self.max_text_length:
            raise ServiceError(
                "text_too_long",
                f"text 长度 {len(value)} 超过上限 {self.max_text_length}",
                400,
                {"length": len(value), "max_length": self.max_text_length},
            )
        return value

    @staticmethod
    def _normalize_options(value: Any, fallback: List[Dict[str, str]]) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        for item in value or fallback:
            if isinstance(item, str):
                option_id = item.strip()
                label = option_id
            elif isinstance(item, dict):
                option_id = str(item.get("id", "")).strip()
                label = str(item.get("label") or option_id).strip()
            else:
                continue
            if option_id and option_id not in {entry["id"] for entry in items}:
                items.append({"id": option_id, "label": label})
        return items or [dict(item) for item in fallback]

    @staticmethod
    def _validate_choice(name: str, value: str, options: List[Dict[str, str]]) -> str:
        normalized = str(value or "").strip()
        allowed = [item["id"] for item in options]
        if normalized not in allowed:
            raise ServiceError(
                f"invalid_{name}",
                f"{name} 不受支持",
                400,
                {name: normalized, "allowed": allowed},
            )
        return normalized

    def _validate_speech_rate(self, value: Any) -> float:
        try:
            rate = float(value)
        except (TypeError, ValueError):
            raise ServiceError("invalid_speech_rate", "speech_rate 必须是数字", 400) from None
        if not self.min_speech_rate <= rate <= self.max_speech_rate:
            raise ServiceError(
                "invalid_speech_rate",
                f"speech_rate 必须在 {self.min_speech_rate} 到 {self.max_speech_rate} 之间",
                400,
                {"speech_rate": rate, "min": self.min_speech_rate, "max": self.max_speech_rate},
            )
        return round(rate, 2)

    async def _position_of(self, task_id: str) -> int:
        snapshot = await self.queue.snapshot()
        for index, item in enumerate(snapshot["items"]):
            if item["task_id"] == task_id:
                return index
        return 0

    async def _worker_loop(self) -> None:
        while True:
            task = await self.queue.get()
            if task is None:
                return
            if task.is_terminal:
                self.history.add(task)
                continue
            self._current = task
            self._cancel = asyncio.Event()
            try:
                await self._run_task(task)
            except asyncio.CancelledError:
                if not task.is_terminal:
                    task.cancel("worker cancelled")
                raise
            except ServiceError as exc:
                task.fail(exc.code, exc.message, exc.detail)
            except Exception as exc:  # pragma: no cover - 兜底
                LOG.exception("任务执行异常 task_id=%s", task.task_id)
                task.fail("internal_error", redact_secret(exc))
            finally:
                if task.phase == "succeeded":
                    self.counters["succeeded"] += 1
                elif task.phase == "failed":
                    self.counters["failed"] += 1
                    self._remember_error(task)
                elif task.phase == "cancelled":
                    self.counters["cancelled"] += 1
                task.remaining_playback_seconds = 0.0
                self.history.add(task)
                self._current = None
                LOG.info(
                    "任务结束 task_id=%s phase=%s total=%.2fs",
                    task.task_id,
                    task.phase,
                    task.total_seconds or 0.0,
                )

    async def _run_task(self, task: TtsTask) -> None:
        cancel = self._cancel

        task.enter("synthesizing")
        result = await self._with_timeout(
            asyncio.to_thread(
                self.provider.synthesize,
                task.text,
                task.voice,
                task.emotion,
                task.speech_rate,
            ),
            self.synthesis_timeout,
            "provider_timeout",
            f"DashScope 合成超时（>{self.synthesis_timeout}s）",
        )
        task.audio = {
            "bytes": result.get("bytes"),
            "model": result.get("model", self.model),
            "voice": result.get("voice", self.voice),
            "emotion": result.get("emotion", task.emotion),
            "speech_rate": result.get("speech_rate", task.speech_rate),
            "request_latency_ms": result.get("latency_ms"),
            "probe": None,
        }
        task.audio["probe"] = await asyncio.to_thread(self.converter.probe, result["data"])
        if cancel.is_set():
            task.cancel("stopped during synthesis")
            return

        task.enter("decoding")
        pcm = await self._with_timeout(
            asyncio.to_thread(self.converter.decode, result["data"]),
            self.decode_timeout,
            "decode_timeout",
            f"MP3 解码超时（>{self.decode_timeout}s）",
        )
        task.pcm = pcm.format_dict()
        task.pcm["chunk_bytes"] = self.converter.chunk_bytes
        task.pcm["chunk_ms"] = self.chunk_ms
        if cancel.is_set():
            task.cancel("stopped during decoding")
            return

        task.enter("publishing")
        priority = self.arbiter.priority("tts_high") if task.priority == "high" or task.interrupt else self.arbiter.priority("tts_normal")
        task.publish = {
            "waiting_for_speaker": True,
            "arbiter_priority": priority,
        }
        try:
            lease = await self.arbiter.acquire(
                "tts",
                task.task_id,
                priority,
                {
                    "task_id": task.task_id,
                    "priority": task.priority,
                    "interrupt": task.interrupt,
                    "phase": "waiting_for_speaker",
                },
                cancel=cancel,
                wait=True,
            )
        except ServiceError as exc:
            if exc.code == "playback_cancelled" or cancel.is_set():
                task.cancel("stopped while waiting for speaker")
                return
            raise

        def on_progress(info: Dict[str, Any]) -> None:
            task.remaining_playback_seconds = float(info.get("remaining_seconds", 0.0))

        combined_cancel = CombinedCancel(cancel, lease.cancel_event)
        lease.metadata["phase"] = "publishing"
        try:
            stats = await self._publish(pcm, combined_cancel, on_progress)
            stats["waiting_for_speaker"] = False
            stats["arbiter_priority"] = priority
            stats["preempted"] = lease.cancel_event.is_set()
            stats["preempt_reason"] = lease.cancel_reason
            task.publish = stats
            if lease.cancel_event.is_set():
                task.cancel(lease.cancel_reason or "preempted")
                return
            if not stats.get("completed"):
                task.cancel("stopped during publishing")
                return

            task.enter("waiting_playback")
            drain = max(0.0, min(self.drain_seconds, pcm.duration_seconds))
            task.publish["drain_seconds"] = drain
            lease.metadata["phase"] = "waiting_playback"
            lease.metadata["drain_seconds"] = drain
            await self._sleep_cancelable(drain, combined_cancel)
            if lease.cancel_event.is_set():
                task.cancel(lease.cancel_reason or "preempted during playback tail")
                return
            if cancel.is_set():
                task.cancel("stopped during playback tail")
                return
            task.remaining_playback_seconds = 0.0
            task.enter("succeeded")
        finally:
            await self.arbiter.release(lease, "tts_released")

    async def _sleep_cancelable(self, seconds: float, cancel: asyncio.Event) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or cancel.is_set():
                return
            await asyncio.sleep(min(0.02, remaining))

    async def _publish(self, pcm, cancel: asyncio.Event, on_progress) -> Dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self.publisher.publish_pcm(
                    pcm,
                    chunk_ms=self.chunk_ms,
                    cancel=cancel,
                    on_progress=on_progress,
                    lead_chunks=self.lead_chunks,
                ),
                timeout=self.publish_timeout,
            )
        except asyncio.TimeoutError:
            raise ServiceError(
                "publish_timeout",
                f"PCM 发布超时（>{self.publish_timeout}s）",
                504,
                {"timeout_seconds": self.publish_timeout},
            ) from None

    async def _with_timeout(self, coro, timeout: float, code: str, message: str):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise ServiceError(code, message, 504, {"timeout_seconds": timeout}) from None
