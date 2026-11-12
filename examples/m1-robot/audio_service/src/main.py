"""audio_service HTTP 入口（默认仅监听 127.0.0.1:8091）。

链路：业务请求 → 本服务 → DashScope → MP3 解码 → 48k/16bit/2ch PCM
      → ROS /audio/speaker_pcm → 厂商 media_audio → 扬声器。
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _configure_ca_bundle() -> None:
    """机器人根文件系统没有 /usr/lib/ssl/cert.pem，默认 CA 库为空。

    DashScope 走 wss://，必须显式指定 CA 包，否则 TLS 握手报
    CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate。
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    candidates = []
    try:
        import certifi

        candidates.append(certifi.where())
    except Exception:  # pragma: no cover - certifi 缺失时退回系统 CA
        pass
    candidates.append("/etc/ssl/certs/ca-certificates.crt")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            os.environ["SSL_CERT_FILE"] = candidate
            os.environ.setdefault("REQUESTS_CA_BUNDLE", candidate)
            os.environ.setdefault("WEBSOCKET_CLIENT_CA_BUNDLE", candidate)
            return


_configure_ca_bundle()

from fastapi import FastAPI, Query  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from state import ServiceError, redact_secret  # noqa: E402
from audio_gateway import AudioGateway  # noqa: E402
from echo_suppression import EchoReferenceSender  # noqa: E402
from api.audio_routes import create_audio_router  # noqa: E402
from api.output_routes import create_output_router  # noqa: E402
from api.vendor_event_routes import create_vendor_event_router  # noqa: E402
from playback_arbiter import PlaybackArbiter  # noqa: E402
from media_audio_domain_bridge import MediaAudioDomainBridge  # noqa: E402
from realtime_output import RealtimeOutputService  # noqa: E402
from ros_pcm_publisher import RosPcmPublisher  # noqa: E402
from ros_runtime import RosRuntime  # noqa: E402
from tts_service import TtsService  # noqa: E402
from vendor_event_bridge import VendorEventBridge  # noqa: E402

LOG = logging.getLogger("audio_service")
CONFIG_PATH = Path(os.environ.get("AUDIO_SERVICE_CONFIG", str(ROOT / "config" / "service.json")))


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise SystemExit(f"配置文件不存在：{CONFIG_PATH}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"配置文件 JSON 非法：{exc}")


def setup_logging(config: dict) -> None:
    service_cfg = config.get("service", {})
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = int(service_cfg.get("log_max_bytes", 5 * 1024 * 1024))
    backups = int(service_cfg.get("log_backup_count", 3))

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(service_cfg.get("log_level", "info")).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "audio_service.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)


CONFIG = load_config()
setup_logging(CONFIG)
RUNTIME = RosRuntime(CONFIG.get("ros", {}), node_name=str(CONFIG.get("ros", {}).get("runtime_node_name", "audio_service")))
GATEWAY = AudioGateway(CONFIG, runtime=RUNTIME)
PUBLISHER = RosPcmPublisher(
    CONFIG.get("ros", {}),
    media_audio_process=str(CONFIG.get("media_audio", {}).get("process_name", "media_audio")),
    runtime=RUNTIME,
    capture_stats=GATEWAY.capture_status,
)
ARBITER = PlaybackArbiter(CONFIG.get("playback", {}))
ECHO_REFERENCE = EchoReferenceSender(CONFIG.get("echo_suppression", {}))
OUTPUT = RealtimeOutputService(CONFIG, PUBLISHER, ARBITER, echo_reference=ECHO_REFERENCE)
MEDIA_AUDIO_BRIDGE = MediaAudioDomainBridge(CONFIG.get("media_audio_bridge", {}), RUNTIME)
VENDOR_EVENTS = VendorEventBridge(
    CONFIG.get("vendor_events", {}),
    runtime=RUNTIME,
    playback_runtime=MEDIA_AUDIO_BRIDGE.media_runtime,
)
SERVICE = TtsService(
    CONFIG,
    publisher=PUBLISHER,
    arbiter=ARBITER,
    manage_publisher=False,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 先建立输入订阅，再启动 TTS 队列；二者共享同一个 ROS Node/Executor。
    await GATEWAY.start()
    await MEDIA_AUDIO_BRIDGE.start()
    await VENDOR_EVENTS.start()
    await asyncio.to_thread(PUBLISHER.start)
    await SERVICE.start()
    try:
        yield
    finally:
        await OUTPUT.stop()
        await ARBITER.stop()
        await SERVICE.stop()
        await asyncio.to_thread(PUBLISHER.stop)
        await VENDOR_EVENTS.stop()
        await MEDIA_AUDIO_BRIDGE.stop()
        await GATEWAY.stop()
        await asyncio.to_thread(RUNTIME.stop)


app = FastAPI(title="M1 audio_service", version=AudioGateway.VERSION, lifespan=lifespan)
app.include_router(create_audio_router(GATEWAY, CONFIG.get("input", {}), OUTPUT))
app.include_router(create_output_router(OUTPUT))
app.include_router(create_vendor_event_router(VENDOR_EVENTS))


class SpeakRequest(BaseModel):
    text: str = ""
    client_request_id: str | None = None
    priority: str = "normal"
    interrupt: bool = False
    voice: str | None = None
    emotion: str = "neutral"
    speech_rate: float = 1.0


@app.exception_handler(ServiceError)
async def service_error_handler(_request, exc: ServiceError):
    payload = exc.as_dict()
    LOG.warning("业务错误 %s: %s", exc.code, exc.message)
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": "请求体格式不正确",
                "detail": {"errors": [redact_secret(item.get("msg")) for item in exc.errors()]},
            }
        },
    )


@app.get("/")
async def index():
    return {
        "service": "audio_service",
        "version": AudioGateway.VERSION,
        "endpoints": [
            "GET /api/health",
            "GET /api/tts/status",
            "POST /api/tts/speak",
            "POST /api/tts/stop",
            "GET /api/tts/history",
            "GET /api/audio/status",
            "GET /api/audio/level",
            "GET /api/audio/sessions",
            "WS /ws/audio/input",
            "WS /ws/audio/output",
            "GET /api/vendor/events/status",
            "WS /ws/vendor/events",
        ],
    }


@app.get("/api/health")
async def health():
    payload = await SERVICE.health()
    input_health = GATEWAY.health()
    payload["input"] = input_health
    if payload.get("status") == "ok" and not input_health.get("online"):
        payload["status"] = "degraded"
    payload["ros_runtime"] = RUNTIME.status()
    payload["playback"] = ARBITER.snapshot()
    payload["output"] = OUTPUT.status()
    payload["vendor_events"] = VENDOR_EVENTS.status()
    payload["media_audio_bridge"] = MEDIA_AUDIO_BRIDGE.status()
    return payload


@app.get("/api/tts/status")
async def tts_status():
    return await SERVICE.status()


@app.post("/api/tts/speak")
async def tts_speak(payload: SpeakRequest):
    result = await SERVICE.speak(
        text=payload.text,
        client_request_id=payload.client_request_id,
        priority=payload.priority,
        interrupt=payload.interrupt,
        voice=payload.voice,
        emotion=payload.emotion,
        speech_rate=payload.speech_rate,
    )
    return JSONResponse(status_code=200 if result.get("deduplicated") else 202, content=result)


@app.post("/api/tts/stop")
async def tts_stop():
    return await SERVICE.stop_playback()


@app.get("/api/tts/history")
async def tts_history(limit: int = Query(20, ge=1, le=100)):
    return SERVICE.history_payload(limit)


@app.get("/api/tts/task/{task_id}")
async def tts_task(task_id: str):
    return SERVICE.get_task(task_id)


def run() -> None:
    import uvicorn

    service_cfg = CONFIG.get("service", {})
    host = str(service_cfg.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        LOG.warning("配置要求只监听回环地址，已将 host=%s 强制改为 127.0.0.1", host)
        host = "127.0.0.1"
    port = int(service_cfg.get("port", 8091))
    LOG.info("启动 uvicorn http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False, lifespan="on")


if __name__ == "__main__":
    run()
