"""Route vendor preset-audio requests through the connected Audio Agent.

The vendor business stack publishes a preset id on
``/audio/speaker_pcm_interrupt``. In the routed deployment ``media_audio``
subscribes in a private ROS domain (or to a separately remapped topic), so this
component can either:

* hand the event to the Audio Agent and acknowledge the vendor behaviour tree;
* ignore it on the Agent's behalf and acknowledge it; or
* forward the original preset id to ``media_audio`` as a fail-safe fallback.

The ROS callback never blocks on the network.  Decisions are made on the
FastAPI asyncio loop and time out to the original vendor behaviour.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional

LOG = logging.getLogger(__name__)


DEFAULT_EVENT_MAP: Dict[str, Dict[str, str]] = {
    "0013": {"event": "vendor_wake", "summary": "检测到厂商唤醒词", "severity": "info", "delivery": "context"},
    "0014": {"event": "vendor_wake", "summary": "检测到厂商唤醒词", "severity": "info", "delivery": "context"},
    "0015": {"event": "vendor_wake", "summary": "检测到厂商唤醒词", "severity": "info", "delivery": "context"},
    "0051": {"event": "self_fall_recovery", "summary": "机器人检测到倒地，正在尝试恢复站立", "severity": "warning", "delivery": "interrupt"},
    "0052": {"event": "stand_up_failed", "summary": "机器人尝试起身失败，需要协助", "severity": "critical", "delivery": "interrupt"},
    "0092": {"event": "map_missing", "summary": "当前任务需要地图，但机器人没有可用地图", "severity": "warning", "delivery": "respond"},
    "0093": {"event": "adapter_connected", "summary": "当前任务无法执行，因为机器人仍连接外部电源适配器", "severity": "warning", "delivery": "respond"},
    "0022": {"event": "leaving_charger", "summary": "机器人准备离开充电桩并开始建图", "severity": "info", "delivery": "context"},
    "0310": {"event": "leaving_charger", "summary": "机器人准备离开充电桩并开始建图", "severity": "info", "delivery": "context"},
    "0021": {"event": "charger_state_error", "summary": "建图前的充电状态不符合预期", "severity": "warning", "delivery": "respond"},
    "0313": {"event": "charger_state_error", "summary": "建图前的充电状态不符合预期", "severity": "warning", "delivery": "respond"},
    "0041": {"event": "visual_mount_started", "summary": "机器人开始执行视觉上车流程", "severity": "info", "delivery": "context"},
    "0048": {"event": "visual_cart_located", "summary": "视觉上车流程已经定位到车体", "severity": "info", "delivery": "context"},
    "0044": {"event": "visual_mount_failed", "summary": "机器人未能完成视觉上车流程", "severity": "warning", "delivery": "respond"},
    "0091": {"event": "task_precondition_failed", "summary": "机器人当前状态不满足任务执行条件", "severity": "warning", "delivery": "respond"},
    "0055": {"event": "tracking_started", "summary": "机器人开始执行目标跟随", "severity": "info", "delivery": "context"},
    "0094": {"event": "study_point_reached", "summary": "机器人已到达学习陪伴点", "severity": "info", "delivery": "context"},
}

FINAL_ACTIONS = frozenset(("agent", "ignore", "play_vendor"))


@dataclass
class AgentSession:
    token: str
    instance_id: str
    queue: asyncio.Queue
    connected_at: float
    last_seen_mono: float
    ready: bool = True


class VendorEventBridge:
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        runtime: Optional[Any] = None,
        playback_runtime: Optional[Any] = None,
    ):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.runtime = runtime
        self.playback_runtime = playback_runtime or runtime
        self.input_topic = str(cfg.get("input_topic", "/audio/speaker_pcm_interrupt"))
        self.playback_topic = str(cfg.get("playback_topic", "/audio/vendor_speaker_pcm_interrupt"))
        self.status_topic = str(cfg.get("speaker_status_topic", "/audio/speaker_pcm_status"))
        self.decision_timeout_seconds = max(0.1, float(cfg.get("decision_timeout_seconds", 1.5)))
        self.agent_stale_seconds = max(1.0, float(cfg.get("agent_stale_seconds", 5.0)))
        self.qos_depth = max(1, int(cfg.get("qos_depth", 10)))
        self.history_size = max(1, int(cfg.get("history_size", 50)))
        self.routing_required = bool(cfg.get("routing_required", True))
        configured_map = cfg.get("event_map", {}) or {}
        self.event_map = {**DEFAULT_EVENT_MAP, **configured_map}

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscription = None
        self._playback_publisher = None
        self._status_publisher = None
        self._started = False
        self._error: Optional[str] = None
        self._agent: Optional[AgentSession] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._history: Deque[Dict[str, Any]] = deque(maxlen=self.history_size)
        self._lock = threading.RLock()
        self._stats = {
            "received": 0,
            "agent_handled": 0,
            "ignored": 0,
            "fallback": 0,
            "timeouts": 0,
            "synthetic_acks": 0,
        }

    async def start(self) -> bool:
        if not self.enabled:
            return False
        if self._started:
            return True
        self._loop = asyncio.get_running_loop()
        try:
            if self.runtime is self.playback_runtime and self.input_topic == self.playback_topic:
                raise RuntimeError("vendor input and playback must use different ROS domains or topics")
            if self.runtime is None or (not self.runtime.available and not self.runtime.start()):
                raise RuntimeError(self.runtime.error if self.runtime is not None else "ROS runtime missing")
            from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
            from std_msgs.msg import Int32, String

            qos = QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=self.qos_depth,
                reliability=ReliabilityPolicy.RELIABLE,
            )
            if self.playback_runtime is None or not self.playback_runtime.available:
                raise RuntimeError("vendor playback ROS runtime unavailable")
            self._playback_publisher = self.playback_runtime.create_publisher(
                String, self.playback_topic, qos
            )
            self._status_publisher = self.runtime.create_publisher(Int32, self.status_topic, qos)
            self._subscription = self.runtime.create_subscription(
                String, self.input_topic, self._on_vendor_prompt, qos
            )
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            LOG.error("厂商语音事件桥接启动失败：%s", self._error)
            return False
        self._started = True
        self._error = None
        LOG.info(
            "厂商语音事件桥接就绪 input=%s playback=%s timeout=%.1fs",
            self.input_topic,
            self.playback_topic,
            self.decision_timeout_seconds,
        )
        return True

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        pending = list(self._pending.items())
        self._pending.clear()
        for _event_id, future in pending:
            if not future.done():
                future.set_result({"action": "play_vendor", "reason": "bridge_stopping"})
        self._agent = None
        for owner, kind, handle in (
            (self.runtime, "subscription", self._subscription),
            (self.playback_runtime, "publisher", self._playback_publisher),
            (self.runtime, "publisher", self._status_publisher),
        ):
            node = owner.node if owner is not None else None
            if node is None or handle is None:
                continue
            try:
                getattr(node, f"destroy_{kind}")(handle)
            except Exception:
                LOG.debug("销毁厂商语音 ROS %s 失败", kind, exc_info=True)
        self._subscription = None
        self._playback_publisher = None
        self._status_publisher = None

    def _on_vendor_prompt(self, message) -> None:
        audio_id = str(getattr(message, "data", "") or "").strip()
        if not audio_id:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            self._publish_vendor(audio_id)
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self._route(audio_id)))

    async def inject(self, audio_id: str) -> Dict[str, Any]:
        """Test/admin entry point that follows the same routing path as ROS."""
        return await self._route(str(audio_id).strip())

    async def _route(self, audio_id: str) -> Dict[str, Any]:
        if not audio_id:
            return {"action": "invalid"}
        self._stats["received"] += 1
        event = self._make_event(audio_id)
        agent = self._active_agent()
        if agent is None:
            return self._fallback(event, "agent_unavailable")

        future = asyncio.get_running_loop().create_future()
        self._pending[event["event_id"]] = future
        try:
            agent.queue.put_nowait(event)
            decision = await asyncio.wait_for(future, timeout=self.decision_timeout_seconds)
        except asyncio.QueueFull:
            return self._fallback(event, "agent_queue_full")
        except asyncio.TimeoutError:
            self._stats["timeouts"] += 1
            return self._fallback(event, "decision_timeout")
        finally:
            self._pending.pop(event["event_id"], None)

        action = decision.get("action")
        event["decision"] = action
        event["decision_reason"] = str(decision.get("reason", ""))[:240]
        event["decided_at"] = round(time.time(), 3)
        if action == "play_vendor":
            return self._fallback(event, event["decision_reason"] or "agent_requested_fallback")
        self._publish_completion()
        key = "ignored" if action == "ignore" else "agent_handled"
        self._stats[key] += 1
        self._remember(event)
        return {"action": action, "event_id": event["event_id"], "audio_id": audio_id}

    def _make_event(self, audio_id: str) -> Dict[str, Any]:
        definition = self.event_map.get(audio_id, {})
        now = time.time()
        return {
            "type": "vendor_audio_event",
            "event_id": f"vendor-{uuid.uuid4().hex[:16]}",
            "audio_id": audio_id,
            "event": str(definition.get("event", "vendor_prompt")),
            "summary": str(definition.get("summary", f"厂商请求播放预设提示音 {audio_id}")),
            "severity": str(definition.get("severity", "warning")),
            "suggested_delivery": str(definition.get("delivery", "respond")),
            "occurred_at": round(now, 3),
        }

    def _fallback(self, event: Dict[str, Any], reason: str) -> Dict[str, Any]:
        event["decision"] = "play_vendor"
        event["decision_reason"] = reason
        event["decided_at"] = round(time.time(), 3)
        self._publish_vendor(event["audio_id"])
        self._stats["fallback"] += 1
        self._remember(event)
        return {"action": "play_vendor", "event_id": event["event_id"], "audio_id": event["audio_id"], "reason": reason}

    def _publish_vendor(self, audio_id: str) -> None:
        publisher = self._playback_publisher
        if publisher is None:
            LOG.error("无法回退播放厂商提示音 %s：播放发布器不可用", audio_id)
            return
        from std_msgs.msg import String

        message = String()
        message.data = audio_id
        publisher.publish(message)
        LOG.info("厂商提示音回退播放 audio_id=%s topic=%s", audio_id, self.playback_topic)

    def _publish_completion(self) -> None:
        publisher = self._status_publisher
        if publisher is None:
            LOG.error("无法确认厂商提示事件完成：状态发布器不可用")
            return
        from std_msgs.msg import Int32

        message = Int32()
        message.data = 1
        publisher.publish(message)
        self._stats["synthetic_acks"] += 1

    def attach_agent(self, instance_id: str) -> AgentSession:
        now_mono = time.monotonic()
        session = AgentSession(
            token=uuid.uuid4().hex,
            instance_id=str(instance_id or "audio-agent")[:128],
            queue=asyncio.Queue(maxsize=32),
            connected_at=time.time(),
            last_seen_mono=now_mono,
        )
        previous = self._agent
        self._agent = session
        if previous is not None:
            try:
                previous.queue.put_nowait({"type": "agent_replaced"})
            except asyncio.QueueFull:
                pass
        return session

    def heartbeat(self, token: str, ready: bool = True) -> bool:
        agent = self._agent
        if agent is None or agent.token != token:
            return False
        agent.last_seen_mono = time.monotonic()
        agent.ready = bool(ready)
        return True

    def detach_agent(self, token: str) -> None:
        agent = self._agent
        if agent is not None and agent.token == token:
            self._agent = None

    def decide(self, token: str, event_id: str, action: str, reason: str = "") -> bool:
        agent = self._active_agent()
        if agent is None or agent.token != token or action not in FINAL_ACTIONS:
            return False
        future = self._pending.get(str(event_id))
        if future is None or future.done():
            return False
        future.set_result({"action": action, "reason": reason})
        agent.last_seen_mono = time.monotonic()
        return True

    def _active_agent(self) -> Optional[AgentSession]:
        agent = self._agent
        if agent is None or not agent.ready:
            return None
        if time.monotonic() - agent.last_seen_mono > self.agent_stale_seconds:
            return None
        return agent

    def _remember(self, event: Dict[str, Any]) -> None:
        self._history.appendleft(dict(event))

    def status(self) -> Dict[str, Any]:
        agent = self._agent
        active = self._active_agent()
        return {
            "enabled": self.enabled,
            "started": self._started,
            "error": self._error,
            "routing_required": self.routing_required,
            "input_topic": self.input_topic,
            "playback_topic": self.playback_topic,
            "speaker_status_topic": self.status_topic,
            "decision_timeout_seconds": self.decision_timeout_seconds,
            "agent": {
                "connected": agent is not None,
                "active": active is not None,
                "instance_id": agent.instance_id if agent else None,
                "ready": agent.ready if agent else False,
                "last_seen_age_seconds": round(time.monotonic() - agent.last_seen_mono, 3) if agent else None,
            },
            "pending": len(self._pending),
            "stats": dict(self._stats),
            "recent_events": list(self._history),
        }
