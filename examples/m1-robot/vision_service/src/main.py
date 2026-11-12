import asyncio
import json
import logging
import logging.handlers
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from service import VisionService


def load_config():
    path = Path(os.environ.get("VISION_SERVICE_CONFIG", ROOT / "config" / "service.json"))
    config = json.loads(path.read_text())
    service = config.get("service", {})
    if service.get("host", "127.0.0.1") not in ("127.0.0.1", "localhost"):
        raise ValueError("vision_service must listen on loopback")
    if not 1 <= int(service.get("port", 8092)) <= 65535:
        raise ValueError("invalid service port")
    processor = config.get("processors", {}).get("object_detection")
    if not isinstance(processor, dict):
        raise ValueError("processors.object_detection is required")
    for key in ("result_topic", "enable_topic"):
        if not str(processor.get(key, "")).startswith("/"):
            raise ValueError(f"object_detection.{key} must be an absolute ROS topic")
    tracking = config.get("processors", {}).get("person_tracking")
    if not isinstance(tracking, dict):
        raise ValueError("processors.person_tracking is required")
    return config


CONFIG = load_config()


def configure_logging():
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "vision_service.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


configure_logging()
SERVICE = VisionService(CONFIG)


@asynccontextmanager
async def lifespan(_app):
    SERVICE.start()
    try:
        yield
    finally:
        SERVICE.stop()


app = FastAPI(title="M1 Vision Service", version=CONFIG["service"].get("version", "0.1.0"), lifespan=lifespan)


@app.get("/api/health")
async def health():
    return SERVICE.health()


@app.get("/api/vision/status")
async def vision_status():
    return SERVICE.health()


@app.get("/api/vision/processors")
async def processors():
    return {"items": SERVICE.processor_statuses()}


@app.post("/api/vision/processors/{processor_id}/start")
async def start_processor(processor_id: str):
    try:
        status = await asyncio.to_thread(SERVICE.start_processor, processor_id)
        return {"ok": True, "processor": status}
    except KeyError:
        raise HTTPException(404, f"unknown processor: {processor_id}")
    except Exception as error:
        raise HTTPException(503, str(error))


@app.post("/api/vision/processors/{processor_id}/stop")
async def stop_processor(processor_id: str):
    try:
        status = await asyncio.to_thread(SERVICE.stop_processor, processor_id)
        return {"ok": True, "processor": status}
    except KeyError:
        raise HTTPException(404, f"unknown processor: {processor_id}")
    except Exception as error:
        raise HTTPException(503, str(error))


@app.get("/api/vision/detections/latest")
async def latest_detection():
    result = SERVICE.bus.latest_result()
    if result is None:
        return JSONResponse(503, {"available": False, "error": "no_detection_result"})
    age = SERVICE.bus.status()["latest_age_seconds"]
    stale_after = float(CONFIG["service"].get("result_stale_after_seconds", 1.5))
    return {
        "available": age is not None and age <= stale_after,
        "stale": age is None or age > stale_after,
        "age_seconds": age,
        "result": result,
    }


@app.get("/api/vision/detections/history")
async def detection_history(limit: int = Query(20, ge=1, le=100)):
    return {"items": SERVICE.bus.recent(limit)}


@app.get("/api/vision/tracks/latest")
async def latest_tracks():
    return await latest_detection()


@app.get("/api/vision/tracks/history")
async def track_history(limit: int = Query(20, ge=1, le=100)):
    return {"items": SERVICE.bus.recent(limit)}


@app.websocket("/ws/vision/events")
async def vision_events(websocket: WebSocket):
    await websocket.accept()
    SERVICE.bus.client_opened()
    last_sequence = 0
    try:
        await websocket.send_json({
            "type": "vision_start",
            "version": CONFIG["service"].get("version", "0.1.0"),
            "processors": SERVICE.processor_statuses(),
        })
        while True:
            event = await asyncio.to_thread(
                SERVICE.bus.wait_for_update, last_sequence, 5.0
            )
            if event is None:
                await websocket.send_json({
                    "type": "vision_heartbeat",
                    "processors": SERVICE.processor_statuses(),
                    "result_bus": SERVICE.bus.status(),
                })
                continue
            last_sequence = event["bus_sequence"]
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        SERVICE.bus.client_closed()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=CONFIG["service"].get("host", "127.0.0.1"),
        port=int(CONFIG["service"].get("port", 8092)),
        log_level="info",
    )
