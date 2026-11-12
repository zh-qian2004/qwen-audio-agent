import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from audio_agent_status import AudioAgentStatus
from audio_monitor import AudioMonitor
from control_audit import ControlAudit
from motion_control import MotionControlError, MotionController
from ros_collector import ROSCollector
from speaker_volume import SpeakerVolume, VolumeError
from state import StateStore
from system_monitor import SystemMonitor
from tts_player import AudioServiceClient, TTSRequestError
from vision_client import VisionServiceClient


ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "dashboard.json").read_text(encoding="utf-8"))
if CONFIG.get("battery_history_log") == "auto":
    CONFIG["battery_history_log"] = "/userdata/log/business/business_task_plugin.log"
COMMAND_CONFIG = json.loads((ROOT / "config" / "vendor_commands.json").read_text(encoding="utf-8"))
STORE = StateStore(CONFIG)
AUDIO = AudioMonitor(
    CONFIG.get("audio", {}),
    on_activity=lambda _payload: STORE.note_topic("audio"),
)
ROS = ROSCollector(STORE)
SYSTEM = SystemMonitor(STORE)
AUDIT = ControlAudit(str(ROOT / "logs" / "motion_control.jsonl"))
MOTION = MotionController(
    COMMAND_CONFIG, STORE, AUDIT,
    business_publisher=ROS.publish_business_request,
)
TTS = AudioServiceClient(CONFIG.get("tts", {}))
VOLUME = SpeakerVolume(CONFIG.get("speaker_volume", {}))
VISION = VisionServiceClient(CONFIG.get("vision", {}))
AUDIO_AGENT = AudioAgentStatus(
    CONFIG.get("audio_agent", {}).get("stale_after_seconds", 6)
)
LOG = logging.getLogger("dashboard")


class MotionCommandRequest(BaseModel):
    command_id: str
    reason: str | None = None
    client_request_id: str | None = None
    duration_ms: int | None = None


class MotionSimpleRequest(BaseModel):
    reason: str | None = None
    client_request_id: str | None = None


class TTSRequest(BaseModel):
    text: str
    speed: int | None = None
    voice: str | None = None
    emotion: str = "neutral"
    speech_rate: float = 1.0
    reason: str | None = None
    client_request_id: str | None = None
    priority: str = "normal"
    interrupt: bool = False


class VolumeRequest(BaseModel):
    percent: int | None = None
    db_gain: float | None = None


class AudioAgentHeartbeat(BaseModel):
    instance_id: str
    label: str | None = None
    deployment_mode: str | None = None
    connector: dict = Field(default_factory=dict)
    gateway: dict = Field(default_factory=dict)


@asynccontextmanager
async def lifespan(_):
    SYSTEM.start()
    ROS.start()
    MOTION.start()
    TTS.start()
    AUDIO.start()
    VISION.start()
    LOG.info("Dashboard started on %s:%d (motion control enabled)", CONFIG["host"], CONFIG["port"])
    try:
        yield
    finally:
        VISION.stop()
        TTS.stop()
        MOTION.shutdown()
        AUDIO.stop()
        ROS.stop()
        SYSTEM.stop()
        LOG.info("Dashboard stopped")


app = FastAPI(title="Zeroth M1 Dashboard", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def _state_payload(include_history=False):
    snapshot = STORE.snapshot(include_history=include_history)
    snapshot["battery_status"] = STORE.battery_status()
    snapshot["vehicle_status"] = STORE.vehicle_status()
    snapshot["motion_control"] = MOTION.status()
    snapshot["tts"] = TTS.cached_status()
    _apply_audio_proxy(snapshot)
    return snapshot


def _apply_audio_proxy(snapshot):
    """麦克风状态改为跟随 audio_service，避免用代理轮询频率冒充真实输入帧率。"""
    audio = AUDIO.snapshot()
    sensor = (snapshot.get("sensors") or {}).get("audio")
    if sensor is None:
        return
    age_ms = audio.get("last_frame_age_ms")
    online = bool(audio.get("online"))
    if online:
        status = "online"
    elif audio.get("error_code"):
        status = "offline"
    else:
        status = "no_data"
    sensor["status"] = status
    sensor["rate_hz"] = audio.get("input_rate_hz")
    sensor["age_seconds"] = round(age_ms / 1000.0, 3) if (online and age_ms is not None) else None
    sensor["source"] = "audio_service"
    topic = sensor.get("topic")
    if isinstance(topic, dict):
        topic["status"] = status
        topic["rate_hz"] = audio.get("input_rate_hz")
        topic["age_seconds"] = sensor["age_seconds"]
        topic["count"] = audio.get("packet_count")


def _actor(request):
    if request is None:
        return "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _control_error(error):
    return JSONResponse(status_code=error.status_code, content={
        "accepted": False,
        "error": error.as_dict(),
    })


def _command_result(result):
    status_code = 200 if result.get("accepted") else 502
    return JSONResponse(status_code=status_code, content=result)


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
async def health():
    snapshot = STORE.snapshot()
    motion = MOTION.status()
    tts = TTS.cached_status()
    return {
        "status": "ok",
        "read_only": False,
        "ros": snapshot["ros"]["status"],
        "uptime_seconds": round(snapshot["service_uptime_seconds"], 3),
        "server_time": snapshot["server_time"],
        "service": snapshot["system"].get("service", {}),
        "bpu": snapshot["system"].get("bpu", {"available": False}),
        "control": {
            "enabled": True,
            "vendor_service": motion["vendor_service"],
            "emergency_active": motion["emergency"]["active"],
        },
        "tts": {
            "enabled": tts["enabled"],
            "backend": "audio_service",
            "backend_url": tts["service"]["url"],
            "backend_online": tts["online"],
            "model": tts["engine"].get("model"),
            "voice": tts["engine"].get("voice"),
            "engine_available": tts["engine"]["available"],
            "topic": tts["engine"].get("topic"),
        },
        "audio": {
            "backend": "audio_service",
            "backend_url": AUDIO.base_url,
            "service_online": AUDIO.service_online,
            "ws_connected": AUDIO.ws_connected,
            "topic": AUDIO.topic,
        },
        "vision": VISION.cached_status(),
    }


@app.get("/api/metadata")
async def metadata():
    return STORE.metadata()


@app.get("/api/state")
async def state():
    return _state_payload(include_history=True)


@app.get("/api/state/current")
async def current_state():
    """Return the live snapshot without the high-volume history series."""
    return _state_payload(include_history=False)


@app.get("/api/vehicle/status")
async def vehicle_status():
    return STORE.vehicle_status()


@app.get("/api/camera/frame")
async def camera_frame():
    jpeg, mono = ROS.get_latest_jpeg()
    if jpeg is None or mono is None:
        return Response(status_code=503, headers={"Cache-Control": "no-store"})
    age = time.monotonic() - mono
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Frame-Age-Seconds": f"{age:.3f}"})


@app.get("/api/vision/status")
async def vision_status():
    return await asyncio.to_thread(VISION.status, True)


@app.get("/api/vision/detections/latest")
async def vision_latest_detection():
    status, data = await asyncio.to_thread(VISION.latest_detection)
    return JSONResponse(status_code=status, content=data)


@app.get("/api/vision/tracks/latest")
async def vision_latest_tracks():
    status, data = await asyncio.to_thread(VISION.latest_tracks)
    return JSONResponse(status_code=status, content=data)


@app.post("/api/vision/processors/object_detection/start")
async def vision_start_detection():
    status, data = await asyncio.to_thread(VISION.control_detection, "start")
    return JSONResponse(status_code=status, content=data)


@app.post("/api/vision/processors/object_detection/stop")
async def vision_stop_detection():
    status, data = await asyncio.to_thread(VISION.control_detection, "stop")
    return JSONResponse(status_code=status, content=data)


@app.get("/api/audio/level")
async def audio_level():
    """代理 audio_service /api/audio/level；服务离线时返回结构化 503。"""
    AUDIO.touch()
    payload = AUDIO.snapshot()
    if not payload.get("proxy", {}).get("service_online"):
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/api/audio/services")
async def audio_services():
    """聚合 audio_service 当前输入、处理、TTS 和播放组件。"""
    payload = await asyncio.to_thread(TTS.audio_services)
    agent = AUDIO_AGENT.snapshot()
    payload["audio_agent"] = agent
    payload.setdefault("summary", {})["agent_online"] = agent["online"]
    payload.setdefault("services", []).append(AUDIO_AGENT.service_card())
    return JSONResponse(status_code=200 if payload.get("online") else 503, content=payload)


@app.get("/api/audio/agent/status")
async def audio_agent_status():
    return AUDIO_AGENT.snapshot()


@app.post("/api/audio/agent/heartbeat")
async def audio_agent_heartbeat(payload: AudioAgentHeartbeat):
    public = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    return AUDIO_AGENT.update(public)


@app.get("/api/tts/status")
async def tts_status():
    return await asyncio.to_thread(TTS.status, True)


@app.post("/api/tts/speak")
async def tts_speak(payload: TTSRequest, request: Request):
    try:
        result = await asyncio.to_thread(
            TTS.speak,
            payload.text,
            payload.speed,
            payload.client_request_id,
            payload.reason,
            _actor(request),
            payload.priority,
            payload.interrupt,
            payload.voice,
            payload.emotion,
            payload.speech_rate,
        )
    except TTSRequestError as error:
        return JSONResponse(status_code=error.status_code, content={
            "accepted": False,
            "error": error.as_dict(),
        })
    return JSONResponse(status_code=202, content=result)


@app.post("/api/tts/stop")
async def tts_stop():
    try:
        result = await asyncio.to_thread(TTS.stop_playback)
    except TTSRequestError as error:
        return JSONResponse(status_code=error.status_code, content={
            "ok": False,
            "error": error.as_dict(),
        })
    return result


@app.get("/api/tts/history")
async def tts_history(limit: int = 10):
    items = await asyncio.to_thread(TTS.history, limit)
    return {"items": items, "backend": "audio_service"}


@app.get("/api/audio/volume")
async def audio_volume():
    return await asyncio.to_thread(VOLUME.status)


@app.post("/api/audio/volume")
async def set_audio_volume(payload: VolumeRequest):
    try:
        if (payload.percent is None) == (payload.db_gain is None):
            raise VolumeError("invalid_volume", "percent 和 db_gain 必须且只能提供一个")
        if payload.db_gain is not None:
            result = await asyncio.to_thread(VOLUME.set_db_gain, payload.db_gain)
        else:
            result = await asyncio.to_thread(VOLUME.set_percent, payload.percent)
    except VolumeError as error:
        return JSONResponse(status_code=error.status_code, content={
            "ok": False,
            "error": error.as_dict(),
        })
    return {"ok": True, **result}


@app.websocket("/ws/audio/pcm")
async def websocket_audio_pcm(websocket: WebSocket):
    """Agent-facing PCM stream, now proxied from audio_service's 20 ms frames."""
    await websocket.accept()
    AUDIO.stream_connected()
    sequence = 0
    metadata_sent_for = None
    try:
        while True:
            snapshot = AUDIO.snapshot()
            if not snapshot.get("proxy", {}).get("service_online"):
                # 先判在线再取缓存帧：避免离线时把上一段音频当成实时流下发
                await websocket.send_json({
                    "type": "audio_unavailable",
                    "code": "audio_service_unavailable",
                    "message": snapshot.get("error_detail") or "audio_service 离线",
                })
                await websocket.close(code=1013)
                return
            packet = AUDIO.packet_after(sequence)
            if packet is None:
                await asyncio.sleep(0.01)
                continue
            sequence = packet["sequence"]
            stream_format = (
                packet["encoding"], packet["sample_rate_hz"],
                packet["bits_per_sample"], packet["channels"], packet["chunk_bytes"],
            )
            if stream_format != metadata_sent_for:
                await websocket.send_json({
                    "type": "audio_start",
                    "encoding": packet["encoding"],
                    "sample_rate_hz": packet["sample_rate_hz"],
                    "bits_per_sample": packet["bits_per_sample"],
                    "channels": packet["channels"],
                    "chunk_bytes": packet["chunk_bytes"],
                    "frame_duration_ms": packet.get("frame_duration_ms") or round(
                        packet["chunk_bytes"] / 2 / packet["channels"]
                        / packet["sample_rate_hz"] * 1000, 3),
                    "delivery": "latest-frame",
                    "source": "audio_service",
                    "proxy_delivery": "pcm_16000_16_1/20ms",
                })
                metadata_sent_for = stream_format
            await websocket.send_bytes(packet["data"])
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        AUDIO.stream_disconnected()


@app.get("/api/motion/capabilities")
async def motion_capabilities():
    return MOTION.capabilities()


@app.get("/api/motion/status")
async def motion_status():
    return MOTION.status()


@app.get("/api/motion/history")
async def motion_history(limit: int = 100):
    return MOTION.history(limit)


@app.post("/api/motion/command")
async def motion_command(payload: MotionCommandRequest, request: Request):
    try:
        result = await asyncio.to_thread(
            MOTION.command, payload.command_id, payload.client_request_id,
            payload.reason, _actor(request),
            payload.duration_ms / 1000.0 if payload.duration_ms is not None else None,
        )
    except MotionControlError as error:
        return _control_error(error)
    return _command_result(result)


@app.post("/api/motion/stop")
async def motion_stop(request: Request, payload: MotionSimpleRequest | None = None):
    payload = payload or MotionSimpleRequest()
    result = await asyncio.to_thread(
        MOTION.stop, payload.reason, _actor(request), payload.client_request_id,
    )
    return _command_result(result)


@app.post("/api/motion/emergency-stop")
async def motion_emergency_stop(request: Request, payload: MotionSimpleRequest | None = None):
    payload = payload or MotionSimpleRequest()
    result = await asyncio.to_thread(MOTION.emergency_stop, payload.reason, _actor(request))
    return _command_result(result)


@app.post("/api/motion/emergency-reset")
async def motion_emergency_reset(request: Request, payload: MotionSimpleRequest | None = None):
    payload = payload or MotionSimpleRequest()
    result = await asyncio.to_thread(MOTION.reset_emergency, payload.reason, _actor(request))
    return _command_result(result)


@app.websocket("/ws/state")
async def websocket_state(websocket: WebSocket):
    await websocket.accept()
    interval = 1.0 / CONFIG["push_rate_hz"]
    try:
        while True:
            started = time.monotonic()
            await websocket.send_json(_state_payload())
            await asyncio.sleep(max(0.001, interval - (time.monotonic() - started)))
    except (WebSocketDisconnect, RuntimeError):
        return


def logging_config():
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "rotating_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "default",
                "filename": str(ROOT / "logs" / "dashboard.log"),
                "maxBytes": 2 * 1024 * 1024,
                "backupCount": 3,
                "encoding": "utf-8",
            },
        },
        "root": {"level": "INFO", "handlers": ["rotating_file"]},
        "loggers": {
            "uvicorn": {"level": "INFO", "handlers": ["rotating_file"], "propagate": False},
            "uvicorn.error": {"level": "INFO", "handlers": ["rotating_file"], "propagate": False},
            "uvicorn.access": {"level": "INFO", "handlers": ["rotating_file"], "propagate": False},
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"],
                log_config=logging_config(), access_log=True)
