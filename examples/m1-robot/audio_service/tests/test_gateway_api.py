"""Gateway 编排、电平计算、HTTP/WS API、Processor 会话与消费者。"""
from __future__ import annotations

import asyncio
import contextlib
import struct
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.audio_routes import _stream_stats, create_audio_router
from audio_gateway import AudioGateway
from input.audio_frame import AudioFormat, make_frame
from input.audio_level import LevelMeter

FMT = AudioFormat(16000, 1, 2)
PACKET_BYTES = 2048


def input_config(**overrides):
    config = {
        "input": {
            "topic": "/audio/capture_pcm",
            "encoding": "pcm_16000_16_1",
            "sample_rate": 16000,
            "channels": 1,
            "sample_width": 2,
            "input_qos_depth": 100,
            "frame_ms": 20,
            "ring_buffer_ms": 5000,
            "ingress_capacity": 32,
            "level_update_hz": 10,
            "consumer_capacity": 100,
            "stream_capacity": 50,
            "pre_roll_max_ms": 5000,
            "mock_sessions": [],
        }
    }
    config["input"].update(overrides)
    return config


def loud_packet(amplitude: int = 8192) -> bytes:
    values = [amplitude, -amplitude] * 512  # 1024 样本 = 2048 字节
    return struct.pack("<1024h", *values)


def raw_frame(sequence: int = 1, amplitude: int = 8192):
    return make_frame(
        sequence=0,
        pcm=loud_packet(amplitude),
        fmt=FMT,
        captured_at_ns=time.monotonic_ns(),
        input_sequence=sequence,
        source_timestamp_ns=1_700_000_000_000_000_000 + sequence * 64_000_000,
    )


def build_app(gateway: AudioGateway) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await gateway.start()
        try:
            yield
        finally:
            await gateway.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_audio_router(gateway, gateway.config))
    return app


async def started_gateway(**overrides) -> AudioGateway:
    gateway = AudioGateway(input_config(**overrides))
    await gateway.start()
    return gateway


# ---- 12. 电平 RMS / Peak ------------------------------------------
def test_level_meter_rms_and_peak():
    meter = LevelMeter(update_hz=1000, history_seconds=10, frame_ms=20)
    meter.update(
        make_frame(sequence=1, pcm=loud_packet(8192), fmt=FMT, captured_at_ns=time.monotonic_ns())
    )
    time.sleep(0.02)
    meter.update(
        make_frame(sequence=2, pcm=loud_packet(8192), fmt=FMT, captured_at_ns=time.monotonic_ns())
    )
    snapshot = meter.snapshot()
    # 8192/32768 = 0.25 → 20*log10(0.25) ≈ -12.04 dBFS
    assert snapshot["level_db"] == pytest.approx(-12.0, abs=0.2)
    assert snapshot["peak_db"] == pytest.approx(-12.0, abs=0.2)
    assert snapshot["active"] is True
    assert snapshot["history"]
    assert snapshot["frame_count"] >= 2


def test_level_meter_silence_and_activity_ratio():
    meter = LevelMeter(update_hz=1000, frame_ms=20, activity_window_seconds=0.1)
    silence = b"\x00\x00" * 320
    for sequence in range(1, 6):
        time.sleep(0.02)
        meter.update(
            make_frame(sequence=sequence, pcm=silence, fmt=FMT, captured_at_ns=time.monotonic_ns())
        )
    snapshot = meter.snapshot()
    assert snapshot["level_db"] is not None and snapshot["level_db"] <= -60.0
    assert snapshot["silence_ratio"] == 1.0
    assert snapshot["activity_ratio"] == 0.0


# ---- Gateway 重分帧与状态 -----------------------------------------
async def test_gateway_rechunks_packets_and_publishes():
    gateway = await started_gateway()
    try:
        for sequence in range(1, 6):
            gateway._process_input(raw_frame(sequence))
        status = gateway.status()
        assert status["rechunker"]["frame_ms"] == 20
        assert status["rechunker"]["frame_bytes"] == 640
        assert status["rechunker"]["output_rate_hz"] == 50.0
        assert status["metrics"]["output_frames"] == 16
        assert status["ring_buffer"]["available_ms"] == 320
        assert status["ring_buffer"]["capacity_ms"] == 5000
        assert status["capture"]["encoding"] == "pcm_16000_16_1"
        assert status["metrics"]["received_packets"] == 0  # 该路径绕过 ROS 回调
        assert gateway.bus.published_frames == 16
    finally:
        await gateway.stop()


# ---- 13. GET /api/audio/status ------------------------------------
def test_api_audio_status_shape():
    gateway = AudioGateway(input_config())
    app = build_app(gateway)
    with TestClient(app) as client:
        gateway._process_input(raw_frame(1))
        response = client.get("/api/audio/status")
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) >= {
            "capture",
            "rechunker",
            "ring_buffer",
            "consumers",
            "metrics",
        }
        assert payload["capture"]["topic"] == "/audio/capture_pcm"
        assert payload["capture"]["encoding"] == "pcm_16000_16_1"
        assert payload["capture"]["sample_rate"] == 16000
        assert payload["capture"]["channels"] == 1
        assert payload["rechunker"]["frame_ms"] == 20
        assert payload["rechunker"]["frame_bytes"] == 640
        assert payload["ring_buffer"]["capacity_ms"] == 5000
        assert payload["metrics"]["output_frames"] >= 3
        assert isinstance(payload["consumers"], list)


# ---- 14. GET /api/audio/level -------------------------------------
def test_api_audio_level_payload():
    gateway = AudioGateway(input_config())
    app = build_app(gateway)
    with TestClient(app) as client:
        gateway.capture._started = True
        gateway.capture.last_packet_mono = time.monotonic()
        gateway._process_input(raw_frame(1, amplitude=8192))
        gateway.level._window_started = time.monotonic() - 5
        gateway._process_input(raw_frame(2, amplitude=8192))
        response = client.get("/api/audio/level")
        assert response.status_code == 200
        payload = response.json()
        assert payload["topic"] == "/audio/capture_pcm"
        assert payload["encoding"] == "pcm_16000_16_1"
        assert payload["sample_rate_hz"] == 16000
        assert payload["sample_format"] == "S16_LE"
        assert payload["channels"] == 1
        assert payload["frame_ms"] == 20
        assert payload["chunk_bytes"] == 640
        assert payload["level_db"] is not None
        assert payload["peak_db"] is not None
        assert isinstance(payload["history"], list)
        assert payload["consumers"] == 0
        assert "stream_clients" in payload


def test_api_audio_level_reports_offline():
    gateway = AudioGateway(input_config())
    app = build_app(gateway)
    with TestClient(app) as client:
        payload = client.get("/api/audio/level").json()
        assert payload["online"] is False
        assert payload["active"] is False
        assert payload["level_db"] is None
        assert payload["error"]


def test_api_audio_status_rejects_invalid_input_config():
    with pytest.raises(ValueError):
        AudioGateway(input_config(frame_ms=0))
    with pytest.raises(ValueError):
        AudioGateway(input_config(ring_buffer_ms=0))
    with pytest.raises(ValueError):
        AudioGateway(input_config(ingress_capacity=0))


# ---- 15. WS 元数据与 640 字节 PCM ---------------------------------
def test_websocket_metadata_and_pcm_frames():
    gateway = AudioGateway(input_config())
    app = build_app(gateway)
    with TestClient(app) as client:
        for sequence in range(1, 3):
            gateway._process_input(raw_frame(sequence))
        with client.websocket_connect("/ws/audio/input?pre_roll_ms=0&delivery=continuous") as ws:
            meta = ws.receive_json()
            assert meta["type"] == "audio_start"
            assert meta["encoding"] == "pcm_16000_16_1"
            assert meta["sample_rate"] == 16000
            assert meta["sample_width"] == 2
            assert meta["channels"] == 1
            assert meta["frame_ms"] == 20
            assert meta["frame_bytes"] == 640
            assert meta["delivery"] == "continuous"
            assert len(meta["stream_id"]) == 32

            gateway._process_input(raw_frame(3))
            data = ws.receive_bytes()
            assert len(data) == 640


def test_stream_stats_expose_low_overhead_capture_timing():
    class Subscription:
        @staticmethod
        def stats():
            return {"dropped_frames": 2, "queue_depth": 1, "lag_ms": 18.5}

    before = time.time_ns() // 1_000_000
    payload = _stream_stats(Subscription(), sent_frames=42, last_sequence=99)
    after = time.time_ns() // 1_000_000

    assert payload["type"] == "audio_stats"
    assert payload["capture_to_robot_send_ms"] == 18.5
    assert before <= payload["server_sent_at_ms"] <= after
    assert payload["ts"] == payload["server_sent_at_ms"] / 1000.0


def test_websocket_pre_roll_seeds_history():
    gateway = AudioGateway(input_config())
    app = build_app(gateway)
    with TestClient(app) as client:
        for sequence in range(1, 6):
            gateway._process_input(raw_frame(sequence))
        with client.websocket_connect("/ws/audio/input?pre_roll_ms=100&delivery=continuous") as ws:
            meta = ws.receive_json()
            assert meta["pre_roll_ms"] == 100
            assert meta["pre_roll_frames"] == 5
            assert len(ws.receive_bytes()) == 640


def test_websocket_invalid_query_is_rejected():
    gateway = AudioGateway(input_config())
    app = build_app(gateway)
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/audio/input?pre_roll_ms=99999"):
                pass
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/audio/input?delivery=bogus"):
                pass


# ---- 16. WS 断开清理 ----------------------------------------------
def test_websocket_disconnect_releases_consumer():
    gateway = AudioGateway(input_config())
    app = build_app(gateway)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/audio/input?delivery=latest") as ws:
            ws.receive_json()
            assert gateway.bus.consumer_count == 1
        deadline = time.monotonic() + 2.0
        while gateway.bus.consumer_count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert gateway.bus.consumer_count == 0


# ---- Mock ASR / Mock TSE 同时消费 ---------------------------------
async def test_mock_asr_and_tse_share_stream_with_preroll():
    gateway = await started_gateway()
    try:
        for sequence in range(1, 6):
            gateway._process_input(raw_frame(sequence))
        asr = await gateway.sessions.start_session(
            "mock_asr", pre_roll_ms=40, policy="fail_on_gap", capacity=100
        )
        tse = await gateway.sessions.start_session(
            "mock_tse", pre_roll_ms=0, policy="drop_oldest", capacity=100
        )
        await asyncio.sleep(0.02)  # 让会话任务完成订阅
        for sequence in range(6, 11):
            gateway._process_input(raw_frame(sequence))
        await asyncio.sleep(0.1)

        asr_snapshot = gateway.sessions.get(asr["session_id"])
        tse_snapshot = gateway.sessions.get(tse["session_id"])
        assert asr_snapshot["state"] == "running"
        assert asr_snapshot["pre_roll_frames"] == 2
        assert asr_snapshot["received_frames"] == 18
        assert asr_snapshot["input_sequence_start"] == 15
        assert asr_snapshot["gap_detected"] is False
        assert asr_snapshot["processor"]["gap_free"] is True

        assert tse_snapshot["received_frames"] == 16
        assert tse_snapshot["processor"]["energy_dbfs"] is not None

        finished = await gateway.sessions.stop_session(asr["session_id"])
        assert finished["state"] == "finished"
        assert finished["processor"]["transcript"].startswith("mock-asr:")
        assert gateway.bus.consumer_count >= 1
    finally:
        await gateway.stop()
    assert gateway.bus.consumer_count == 0


async def test_session_gap_marks_session_and_keeps_gateway_running():
    gateway = await started_gateway()
    try:
        session = await gateway.sessions.start_session(
            "mock_asr", pre_roll_ms=0, policy="fail_on_gap", capacity=10
        )
        await asyncio.sleep(0.02)  # 让会话任务完成订阅
        gateway._process_input(raw_frame(1))  # 3 帧：1,2,3
        await asyncio.sleep(0.05)
        # 直接注入序号跳变，模拟入口丢包
        gateway.bus.publish(
            make_frame(sequence=50, pcm=b"\x00\x00" * 320, fmt=FMT, captured_at_ns=time.monotonic_ns())
        )
        await asyncio.sleep(0.05)
        snapshot = gateway.sessions.get(session["session_id"])
        assert snapshot["state"] == "gapped"
        assert snapshot["gap_detected"] is True
        assert snapshot["error"]["code"] == "audio_gap"
        # Gateway 继续可用
        gateway._process_input(raw_frame(2))
        assert gateway.status()["metrics"]["output_frames"] > 3
        assert gateway.sessions.available_types == ["mock_asr", "mock_tse"]
    finally:
        await gateway.stop()


async def test_session_rejects_invalid_pre_roll_and_type():
    gateway = await started_gateway()
    try:
        with pytest.raises(ValueError):
            await gateway.sessions.start_session("mock_asr", pre_roll_ms=6000)
        with pytest.raises(ValueError):
            await gateway.sessions.start_session("unknown-provider")
    finally:
        await gateway.stop()
