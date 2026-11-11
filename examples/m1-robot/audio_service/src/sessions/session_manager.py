"""处理会话管理：把 Audio Bus 订阅、pre-roll 与 Processor 生命周期串起来。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from input.audio_bus import AudioGapError, POLICIES
from processors.mock_asr import MockAsrProcessor
from processors.mock_tse import MockTseProcessor
from sessions.audio_session import AudioSession

LOG = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, gateway, max_sessions: int = 20):
        self.gateway = gateway
        self.max_sessions = max(1, int(max_sessions))
        self._factories = {
            "mock_asr": MockAsrProcessor,
            "mock_tse": MockTseProcessor,
        }
        self._sessions: Dict[str, AudioSession] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._processors: Dict[str, Any] = {}
        self._closed = False

    def register(self, processor_type: str, factory) -> None:
        self._factories[str(processor_type)] = factory

    @property
    def available_types(self) -> List[str]:
        return sorted(self._factories)

    async def start_session(
        self,
        processor_type: str,
        pre_roll_ms: int = 300,
        policy: str = "fail_on_gap",
        capacity: int = 100,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self._closed:
            raise RuntimeError("SessionManager 已关闭")
        factory = self._factories.get(processor_type)
        if factory is None:
            raise ValueError(f"未知 Processor 类型 {processor_type}，可用 {self.available_types}")
        if policy not in POLICIES:
            raise ValueError(f"未知消费策略 {policy}，可选 {POLICIES}")
        pre_roll_ms = int(pre_roll_ms)
        if pre_roll_ms < 0 or pre_roll_ms > self.gateway.pre_roll_max_ms:
            raise ValueError(
                f"pre_roll_ms 需在 0..{self.gateway.pre_roll_max_ms}，收到 {pre_roll_ms}"
            )
        capacity = max(1, int(capacity))

        session = AudioSession(
            processor_type=processor_type,
            pre_roll_ms=pre_roll_ms,
            policy=policy,
            capacity=capacity,
            params=params,
        )
        processor = factory()
        self._sessions[session.session_id] = session
        self._processors[session.session_id] = processor
        self._evict()
        session.mark_started()
        self._tasks[session.session_id] = asyncio.create_task(
            self._run(session, processor), name=f"audio-session-{session.session_id}"
        )
        LOG.info(
            "音频会话启动 session=%s type=%s policy=%s pre_roll=%dms",
            session.session_id,
            processor_type,
            policy,
            pre_roll_ms,
        )
        return session.snapshot(processor.status())

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        task = self._tasks.get(session_id)
        if task is not None:
            session.stop_requested = True
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        processor = self._processors.get(session_id)
        return session.snapshot(processor.status() if processor is not None else None)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        LOG.info("音频会话管理器已关闭")

    async def _run(self, session: AudioSession, processor) -> None:
        subscription = self.gateway.subscribe(
            f"session-{session.session_id}", policy=session.policy, capacity=session.capacity
        )
        try:
            if session.pre_roll_ms > 0:
                frames = self.gateway.ring.recent(session.pre_roll_ms)
                session.pre_roll_frames = subscription.seed(frames)
            await processor.start(session)
            async for frame in subscription:
                session.note_frame(frame.sequence)
                await processor.accept(frame)
            await processor.finish()
            session.mark_finished()
        except AudioGapError as exc:
            session.mark_gap(exc.event)
            LOG.warning("会话音频缺口 session=%s reason=%s", session.session_id, exc.event.get("reason"))
            await processor.cancel()
        except asyncio.CancelledError:
            if session.stop_requested:
                # 主动停止：走正常收尾，让 Processor 产出最终结果
                await processor.finish()
                session.mark_finished()
            else:
                session.mark_cancelled("session stopped")
                await processor.cancel()
            raise
        except Exception as exc:  # pragma: no cover - Provider 异常兜底
            LOG.exception("会话处理异常 session=%s", session.session_id)
            session.mark_failed("processor_error", f"{type(exc).__name__}: {exc}")
            await processor.cancel()
        finally:
            session.dropped_frames = subscription.metrics["dropped_frames"]
            subscription.close()
            self._tasks.pop(session.session_id, None)

    def _evict(self) -> None:
        if len(self._sessions) <= self.max_sessions:
            return
        finished = [
            item
            for item in self._sessions.values()
            if item.session_id not in self._tasks
        ]
        finished.sort(key=lambda item: item.created_at)
        for session in finished[: max(0, len(self._sessions) - self.max_sessions)]:
            self._sessions.pop(session.session_id, None)
            self._processors.pop(session.session_id, None)

    def snapshot(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        sessions = sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)
        if limit:
            sessions = sessions[: max(1, int(limit))]
        return [
            session.snapshot(
                self._processors[session.session_id].status()
                if session.session_id in self._processors
                else None
            )
            for session in sessions
        ]

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        processor = self._processors.get(session_id)
        return session.snapshot(processor.status() if processor is not None else None)
