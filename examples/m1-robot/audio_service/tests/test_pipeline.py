"""完整链路：Provider 成功/错误/超时、解码失败、状态转换、停止、脱敏。"""
from __future__ import annotations

import pytest

from conftest import FakeConverter, FakeProvider, FakePublisher, base_config, make_service, wait_terminal
from dashscope_provider import DashScopeProvider
from state import ServiceError, TtsTask, redact_secret


async def test_successful_pipeline_phase_order():
    publisher = FakePublisher()
    service = make_service(publisher=publisher)
    await service.start()
    try:
        result = await service.speak("你好")
        task = await wait_terminal(service, result["task_id"])
    finally:
        await service.stop()

    assert task["phase"] == "succeeded"
    assert list(task["timestamps"]) == [
        "queued",
        "synthesizing",
        "decoding",
        "publishing",
        "waiting_playback",
        "succeeded",
    ]
    assert task["pcm"]["sample_rate"] == 48000
    assert task["pcm"]["channels"] == 2
    assert task["pcm"]["bytes"] == int(48000 * 0.24) * 4
    assert task["publish"]["packets"] == 4
    assert task["publish"]["chunk_bytes"] == 11520
    assert task["publish"]["media_audio_pid_unchanged"] is True
    assert task["audio"]["bytes"] == len(b"ID3fake-mp3")
    assert task["voice"] == "longanhuan_v3.1"
    assert task["emotion"] == "neutral"
    assert task["speech_rate"] == 1.0
    assert task["error"] is None
    assert service.counters["succeeded"] == 1


async def test_provider_error_marks_task_failed():
    provider = FakeProvider(error=ServiceError("provider_error", "鉴权失败", 502))
    service = make_service(provider=provider)
    await service.start()
    try:
        result = await service.speak("你好")
        task = await wait_terminal(service, result["task_id"])
    finally:
        await service.stop()

    assert task["phase"] == "failed"
    assert task["error"]["code"] == "provider_error"
    status = await service.status()
    assert status["last_error"]["code"] == "provider_error"
    assert service.counters["failed"] == 1


async def test_provider_timeout_is_reported():
    provider = FakeProvider(delay=0.3)
    service = make_service(config=base_config(tts={"synthesis_timeout_seconds": 0.05}), provider=provider)
    await service.start()
    try:
        result = await service.speak("你好")
        task = await wait_terminal(service, result["task_id"])
    finally:
        await service.stop()

    assert task["phase"] == "failed"
    assert task["error"]["code"] == "provider_timeout"
    assert task["error"]["detail"]["timeout_seconds"] == 0.05


async def test_decode_failure_is_reported():
    converter = FakeConverter(error=ServiceError("decode_failed", "MP3 解码失败", 502))
    service = make_service(converter=converter)
    await service.start()
    try:
        result = await service.speak("你好")
        task = await wait_terminal(service, result["task_id"])
    finally:
        await service.stop()

    assert task["phase"] == "failed"
    assert task["error"]["code"] == "decode_failed"


async def test_ros_unavailable_fails_task():
    publisher = FakePublisher(error=ServiceError("ros_unavailable", "ROS 不可用", 503))
    service = make_service(publisher=publisher)
    await service.start()
    try:
        result = await service.speak("你好")
        task = await wait_terminal(service, result["task_id"])
    finally:
        await service.stop()

    assert task["phase"] == "failed"
    assert task["error"]["code"] == "ros_unavailable"


async def test_stop_clears_queue_and_cancels_current():
    publisher = FakePublisher(chunk_delay=0.05)
    converter = FakeConverter(duration_seconds=1.2)
    service = make_service(converter=converter, publisher=publisher)
    await service.start()
    try:
        first = await service.speak("第一段")
        second = await service.speak("第二段")
        third = await service.speak("第三段")

        for _ in range(200):
            if service.get_task(first["task_id"])["phase"] == "publishing":
                break
            await asyncio_sleep(0.01)

        result = await service.stop_playback()
        assert result["cancelled_queued"] == 2
        assert result["current_task_id"] == first["task_id"]

        first_task = await wait_terminal(service, first["task_id"])
        second_task = await wait_terminal(service, second["task_id"])
        third_task = await wait_terminal(service, third["task_id"])
    finally:
        await service.stop()

    assert first_task["phase"] == "cancelled"
    assert second_task["phase"] == "cancelled"
    assert third_task["phase"] == "cancelled"
    assert second_task["error"]["code"] == "cancelled"
    assert first_task["publish"]["completed"] is False
    assert sum(publisher.packets) < 1.2 * 48000 * 4


async def test_stop_without_active_task_is_safe():
    service = make_service()
    result = await service.stop_playback()
    assert result["ok"] is True
    assert result["cancelled_queued"] == 0
    assert result["current_task_id"] is None
    assert result["playback_control"]["enabled"] is False


async def test_history_and_status_payload():
    service = make_service()
    await service.start()
    try:
        result = await service.speak("你好")
        await wait_terminal(service, result["task_id"])
        status = await service.status()
        health = await service.health()
        history = service.history_payload(5)
    finally:
        await service.stop()

    assert status["model"] == "qwen-audio-3.1-tts-flash"
    assert status["voice"] == "longanhuan_v3.1"
    assert status["media_audio"]["process"] == "media_audio"
    assert status["ros"]["topic"] == "/audio/speaker_pcm"
    assert status["queue_capacity"] == 3
    assert health["status"] in ("ok", "degraded")
    assert history["items"][0]["phase"] == "succeeded"
    assert history["items"][0]["total_seconds"] >= 0


async def test_api_key_never_exposed():
    raw_key = "sk-1234567890abcdef1234567890abcdef"
    provider = DashScopeProvider(
        {"model": "m", "voice": "v", "api_key_env": "DASHSCOPE_API_KEY"},
        api_key=raw_key,
        synthesizer_factory=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(f"auth failed for {raw_key}")),
    )
    status = provider.status()
    assert raw_key not in str(status)
    assert status["api_key_masked"] == "sk-123****"
    assert status["api_key_configured"] is True

    with pytest.raises(ServiceError) as info:
        provider.synthesize("你好")
    assert raw_key not in info.value.message
    assert "sk-123****" in info.value.message
    assert raw_key not in str(provider.status())

    assert raw_key not in redact_secret(f"token={raw_key}")
    assert "sk-123****" in redact_secret(f"token={raw_key}")
    workspace_key = "sk-ws-AAAA.BBBB.CCCC.DDDDEEEEFFFFGGGGHHHHIIII"
    assert workspace_key not in redact_secret(f"auth {workspace_key} failed")
    assert "sk-ws-****" in redact_secret(f"auth {workspace_key} failed")


async def test_voice_emotion_and_rate_reach_provider():
    provider = FakeProvider()
    service = make_service(provider=provider)
    await service.start()
    try:
        result = await service.speak(
            "出发吧",
            voice="xunanchuan_v3.1",
            emotion="excited",
            speech_rate=1.25,
        )
        task = await wait_terminal(service, result["task_id"])
    finally:
        await service.stop()

    assert provider.calls[0] == {
        "text": "出发吧",
        "voice": "xunanchuan_v3.1",
        "emotion": "excited",
        "speech_rate": 1.25,
    }
    assert task["audio"]["voice"] == "xunanchuan_v3.1"
    assert task["audio"]["emotion"] == "excited"
    assert task["audio"]["speech_rate"] == 1.25


def test_dashscope_provider_maps_controls_to_sdk_and_text_tag():
    captured = {}

    class Synthesizer:
        def call(self, text):
            captured["text"] = text
            return b"mp3"

    def factory(**kwargs):
        captured["kwargs"] = kwargs
        return Synthesizer()

    provider = DashScopeProvider(
        {"model": "m", "voice": "default", "api_key_env": "DASHSCOPE_API_KEY"},
        api_key="sk-test-value",
        synthesizer_factory=factory,
    )
    result = provider.synthesize(
        "机器人已就绪",
        voice="custom",
        emotion="serious",
        speech_rate=0.85,
    )

    assert captured["kwargs"] == {"model": "m", "voice": "custom", "speech_rate": 0.85}
    assert captured["text"] == "[serious]机器人已就绪"
    assert result["voice"] == "custom"
    assert result["emotion"] == "serious"


async def test_invalid_phase_is_rejected():
    task = TtsTask("x")
    with pytest.raises(ValueError):
        task.enter("unknown")
    task.enter("synthesizing")
    task.fail("provider_error", "boom")
    assert task.phase == "failed"
    assert task.is_terminal


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
