"""Selective ROS-domain bridge that isolates the vendor interrupt subscriber.

``media_audio`` runs in a private ROS domain.  Only the audio topics required by
the rest of the robot are relayed.  The vendor interrupt topic is intentionally
not relayed; :mod:`vendor_event_bridge` publishes it in the private domain only
after the Audio Agent has made a decision.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from ros_runtime import RosRuntime

LOG = logging.getLogger(__name__)


class MediaAudioDomainBridge:
    def __init__(
        self,
        config: Optional[Dict[str, Any]],
        host_runtime: Any,
        media_runtime: Optional[Any] = None,
    ):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.host_runtime = host_runtime
        self.domain_id = int(cfg.get("domain_id", 43))
        self.qos_depth = max(1, int(cfg.get("qos_depth", 100)))
        self.service_timeout_seconds = max(0.1, float(cfg.get("service_timeout_seconds", 3.0)))
        self.media_runtime = media_runtime or RosRuntime(
            {
                "domain_id": self.domain_id,
                "executor_threads": max(1, int(cfg.get("executor_threads", 2))),
            },
            node_name=str(cfg.get("node_name", "audio_service_media_bridge")),
        )
        self._handles: List[Tuple[Any, str, Any]] = []
        self._started = False
        self._error: Optional[str] = None
        self._stats: Dict[str, int] = {}
        self._service_stats: Dict[str, Dict[str, int]] = {}
        self._service_lock = threading.RLock()
        self._pending_services: Dict[int, Dict[str, Any]] = {}
        self._service_sequence = 0
        self._service_clock = None
        self._service_callback_group = None
        self._stopping = False

    async def start(self) -> bool:
        if not self.enabled:
            return False
        if self._started:
            return True
        self._stopping = False
        try:
            if not self.host_runtime.available and not self.host_runtime.start():
                raise RuntimeError(self.host_runtime.error or "host ROS runtime unavailable")
            if not self.media_runtime.start():
                raise RuntimeError(self.media_runtime.error or "media ROS runtime unavailable")
            self._create_routes()
            self._create_service_routes()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            LOG.error("media_audio 跨域桥接启动失败：%s", self._error)
            self._destroy_handles()
            self.media_runtime.stop()
            return False
        self._started = True
        self._error = None
        LOG.info(
            "media_audio 跨域桥接就绪 private_domain=%d routes=%d services=%d",
            self.domain_id, len(self._stats), len(self._service_stats),
        )
        return True

    def _create_routes(self) -> None:
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image
        from std_msgs.msg import Int32, Int32MultiArray, String

        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=self.qos_depth,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        routes = (
            (self.media_runtime, self.host_runtime, Image, "/audio/capture_pcm"),
            (self.media_runtime, self.host_runtime, Image, "/audio/capture_pcm_origin"),
            (self.media_runtime, self.host_runtime, Int32MultiArray, "/audio/kw_detect"),
            (self.media_runtime, self.host_runtime, Int32, "/audio/speaker_pcm_status"),
            (self.media_runtime, self.host_runtime, String, "/audio/play_tts_react"),
            (self.host_runtime, self.media_runtime, Image, "/audio/speaker_pcm"),
            (self.host_runtime, self.media_runtime, String, "/audio/play_tts"),
            (self.host_runtime, self.media_runtime, String, "/audio/speaker_control"),
            (self.host_runtime, self.media_runtime, String, "/SystemWiFiStatus"),
        )
        for source, target, message_type, topic in routes:
            key = f"{source.node_name}->{target.node_name}:{topic}"
            self._stats[key] = 0
            publisher = target.create_publisher(message_type, topic, qos)

            def relay(message, *, output=publisher, route_key=key):
                output.publish(message)
                self._stats[route_key] += 1

            subscription = source.create_subscription(message_type, topic, relay, qos)
            self._handles.extend(((target, "publisher", publisher), (source, "subscription", subscription)))

    def _create_service_routes(self) -> None:
        from rclpy.callback_groups import ReentrantCallbackGroup
        from rclpy.clock import Clock, ClockType
        from std_srvs.srv import Trigger
        from video_ros_msgs.srv import VolumeControl

        # Service handlers suspend on ROS futures.  A reentrant group lets PCM
        # callbacks, client replies and timeout timers run while a call is pending.
        self._service_callback_group = ReentrantCallbackGroup()
        self._service_clock = Clock(clock_type=ClockType.STEADY_TIME)
        routes = (
            (Trigger, "/audio_enable"),
            (Trigger, "/audio_disable"),
            (Trigger, "/speaker_enable"),
            (Trigger, "/speaker_disable"),
            (VolumeControl, "/speaker_volume_control"),
            (Trigger, "/audio/set_mode_aec3"),
            (Trigger, "/audio/set_mode_anc"),
            (Trigger, "/audio/set_mode_mic3"),
        )
        # /all_enable and /all_disable also exist in the default domain's video
        # manager.  Advertising another server there would route calls randomly.
        for service_type, name in routes:
            client = self.media_runtime.node.create_client(
                service_type, name, callback_group=self._service_callback_group
            )
            self._handles.append((self.media_runtime, "client", client))
            self._service_stats[name] = {
                "requested": 0, "completed": 0, "timeouts": 0,
                "unavailable": 0, "errors": 0, "stopped": 0,
            }

            async def forward(request, response, *, target=client, service_name=name):
                return await self._forward_service(target, service_name, request, response)

            server = self.host_runtime.node.create_service(
                service_type, name, forward, callback_group=self._service_callback_group
            )
            self._handles.append((self.host_runtime, "service", server))

    async def _forward_service(self, client, name: str, request, response):
        from rclpy.task import Future

        result = Future()
        with self._service_lock:
            self._service_sequence += 1
            call_id = self._service_sequence
            pending = {
                "result": result, "response": response, "client": client,
                "request_future": None, "timer": None, "name": name,
            }
            self._pending_services[call_id] = pending
            self._service_stats[name]["requested"] += 1
            try:
                if self._stopping:
                    self._finish_service_call(call_id, "stopped", "audio bridge is stopping")
                elif not client.service_is_ready():
                    self._finish_service_call(call_id, "unavailable", f"media_audio service {name} unavailable")
                else:
                    pending["timer"] = self.host_runtime.node.create_timer(
                        self.service_timeout_seconds,
                        lambda: self._finish_service_call(
                            call_id, "timeouts",
                            f"media_audio service {name} timed out after {self.service_timeout_seconds:g}s",
                        ),
                        callback_group=self._service_callback_group,
                        clock=self._service_clock,
                    )
                    pending["request_future"] = client.call_async(request)
                    pending["request_future"].add_done_callback(
                        lambda future: self._service_reply(call_id, future)
                    )
            except Exception as exc:
                self._finish_service_call(call_id, "errors", f"{type(exc).__name__}: {exc}")
        # This is a rclpy Future awaited by the ROS executor, not an asyncio
        # Future or a blocking wait in one of the capture executor threads.
        return await result

    def _service_reply(self, call_id: int, future) -> None:
        try:
            if future.cancelled():
                self._finish_service_call(call_id, "errors", "media_audio request cancelled")
                return
            response = future.result()
            if response is None:
                raise RuntimeError("media_audio returned no service response")
        except Exception as exc:
            self._finish_service_call(call_id, "errors", f"{type(exc).__name__}: {exc}")
            return
        self._finish_service_call(call_id, "completed", response=response)

    def _finish_service_call(self, call_id: int, outcome: str, message: str = "", response=None) -> None:
        with self._service_lock:
            pending = self._pending_services.pop(call_id, None)
            if pending is None:
                return  # A timeout, reply, or shutdown already won this race.
            self._service_stats[pending["name"]][outcome] += 1

        timer = pending["timer"]
        if timer is not None:
            try:
                timer.cancel()
                self.host_runtime.node.destroy_timer(timer)
            except Exception:
                LOG.debug("销毁音频服务超时计时器失败", exc_info=True)
        request_future = pending["request_future"]
        if outcome != "completed" and request_future is not None:
            try:
                pending["client"].remove_pending_request(request_future)
                request_future.cancel()
            except Exception:
                LOG.debug("清理音频服务请求失败", exc_info=True)
        if response is None:
            response = pending["response"]
            if hasattr(response, "success"):
                response.success = False
            if hasattr(response, "message"):
                response.message = message
            LOG.warning("音频服务跨域请求失败 service=%s reason=%s", pending["name"], message)
        pending["result"].set_result(response)

    async def stop(self) -> None:
        if not self._started and not self.media_runtime.available:
            return
        self._started = False
        self._stopping = True
        with self._service_lock:
            pending = list(self._pending_services)
        for call_id in pending:
            self._finish_service_call(call_id, "stopped", "audio bridge is stopping")
        self._destroy_handles()
        self.media_runtime.stop()

    def _destroy_handles(self) -> None:
        for runtime, kind, handle in reversed(self._handles):
            node = runtime.node
            if node is None:
                continue
            try:
                getattr(node, f"destroy_{kind}")(handle)
            except Exception:
                LOG.debug("销毁跨域桥接 %s 失败", kind, exc_info=True)
        self._handles.clear()

    def status(self) -> Dict[str, Any]:
        with self._service_lock:
            services = {name: dict(values) for name, values in self._service_stats.items()}
            pending = len(self._pending_services)
        return {
            "enabled": self.enabled,
            "started": self._started,
            "private_domain_id": self.domain_id,
            "error": self._error,
            "runtime": self.media_runtime.status(),
            "routes": dict(self._stats),
            "service_timeout_seconds": self.service_timeout_seconds,
            "pending_services": pending,
            "services": services,
        }
