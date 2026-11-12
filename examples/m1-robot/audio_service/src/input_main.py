"""Isolated microphone WebSocket service for reliable full-duplex barge-in."""
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

from fastapi import FastAPI  # noqa: E402

from api.audio_routes import create_audio_router  # noqa: E402
from audio_gateway import AudioGateway  # noqa: E402
from echo_suppression import EchoSuppressor  # noqa: E402
from ros_runtime import RosRuntime  # noqa: E402

CONFIG_PATH = Path(os.environ.get("AUDIO_SERVICE_CONFIG", str(ROOT / "config" / "service.json")))
with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
    CONFIG = json.load(handle)

log_dir = ROOT / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
handler = logging.handlers.RotatingFileHandler(
    log_dir / "input_service.log",
    maxBytes=int(CONFIG.get("service", {}).get("log_max_bytes", 5 * 1024 * 1024)),
    backupCount=int(CONFIG.get("service", {}).get("log_backup_count", 3)),
    encoding="utf-8",
)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(handler)
LOG = logging.getLogger("audio_input_service")

runtime_config = dict(CONFIG.get("ros", {}))
runtime_config["executor_threads"] = 1
RUNTIME = RosRuntime(runtime_config, node_name="audio_service_input")
ECHO_SUPPRESSOR = EchoSuppressor(CONFIG.get("echo_suppression", {}))
GATEWAY = AudioGateway(CONFIG, runtime=RUNTIME, echo_suppressor=ECHO_SUPPRESSOR)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await GATEWAY.start()
    try:
        yield
    finally:
        await GATEWAY.stop()
        await asyncio.to_thread(RUNTIME.stop)


app = FastAPI(title="M1 audio input service", version=AudioGateway.VERSION, lifespan=lifespan)
app.include_router(create_audio_router(GATEWAY, CONFIG.get("input", {})))


@app.get("/api/health")
async def health():
    input_health = GATEWAY.health()
    return {
        "service": "audio_input_service",
        "status": "ok" if input_health.get("online") else "degraded",
        "version": AudioGateway.VERSION,
        "input": input_health,
        "ros_runtime": RUNTIME.status(),
    }


def run() -> None:
    import uvicorn

    port = int(os.environ.get("AUDIO_INPUT_SERVICE_PORT", "8093"))
    LOG.info("启动独立麦克风服务 http://127.0.0.1:%d", port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="on")


if __name__ == "__main__":
    run()
