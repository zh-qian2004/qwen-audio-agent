"""文本校验、幂等、队列容量与优先级。"""
from __future__ import annotations

import json

import pytest

from conftest import ROOT, base_config, make_service
from state import ServiceError


async def test_empty_text_rejected():
    service = make_service()
    with pytest.raises(ServiceError) as info:
        await service.speak("   ")
    assert info.value.code == "invalid_text"
    assert info.value.status_code == 400


async def test_none_text_rejected():
    service = make_service()
    with pytest.raises(ServiceError) as info:
        await service.speak(None)
    assert info.value.code == "invalid_text"


async def test_text_too_long_rejected():
    service = make_service(config=base_config(tts={"max_text_length": 5}))
    with pytest.raises(ServiceError) as info:
        await service.speak("一二三四五六")
    assert info.value.code == "text_too_long"
    assert info.value.detail["max_length"] == 5


async def test_invalid_priority_rejected():
    service = make_service()
    with pytest.raises(ServiceError) as info:
        await service.speak("你好", priority="urgent")
    assert info.value.code == "invalid_priority"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"voice": "unknown"}, "invalid_voice"),
        ({"emotion": "happy"}, "invalid_emotion"),
        ({"speech_rate": 0.49}, "invalid_speech_rate"),
        ({"speech_rate": 2.01}, "invalid_speech_rate"),
    ],
)
async def test_invalid_voice_emotion_and_rate_rejected(kwargs, code):
    service = make_service()
    with pytest.raises(ServiceError) as info:
        await service.speak("你好", **kwargs)
    assert info.value.code == code


async def test_control_capabilities_are_reported():
    service = make_service()
    status = await service.status()
    controls = status["controls"]
    assert [item["id"] for item in controls["voices"]] == ["longanhuan_v3.1", "xunanchuan_v3.1"]
    assert [item["id"] for item in controls["emotions"]] == ["neutral", "excited"]
    assert controls["speech_rate"] == {"min": 0.5, "max": 2.0, "step": 0.05, "default": 1.0}


def test_shipped_config_lists_ten_chinese_voices():
    config = json.loads((ROOT / "config" / "service.json").read_text(encoding="utf-8"))
    voices = config["tts"]["voices"]
    ids = [item["id"] for item in voices]
    assert len(ids) == 10
    assert len(set(ids)) == 10
    assert config["tts"]["voice"] in ids
    assert all(item["label"].strip() for item in voices)


async def test_text_is_trimmed():
    service = make_service()
    result = await service.speak("  你好  ")
    assert result["task"]["text"] == "你好"


async def test_idempotent_client_request_id():
    service = make_service()
    first = await service.speak("你好", client_request_id="req-1")
    second = await service.speak("你好", client_request_id="req-1")
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["task_id"] == first["task_id"]
    assert service.counters["deduplicated"] == 1


async def test_different_client_request_ids_create_new_tasks():
    service = make_service()
    first = await service.speak("你好", client_request_id="req-1")
    second = await service.speak("你好", client_request_id="req-2")
    assert first["task_id"] != second["task_id"]


async def test_queue_capacity_limit():
    # worker 未启动，任务全部滞留在队列里
    service = make_service(config=base_config(queue={"capacity": 2}))
    await service.speak("一")
    await service.speak("二")
    with pytest.raises(ServiceError) as info:
        await service.speak("三")
    assert info.value.code == "queue_full"
    assert info.value.status_code == 429
    assert info.value.detail["capacity"] == 2


async def test_high_priority_task_jumps_queue():
    service = make_service(config=base_config(queue={"capacity": 4}))
    normal = await service.speak("普通一")
    await service.speak("普通二")
    urgent = await service.speak("插播", priority="high")
    snapshot = await service.queue.snapshot()
    ids = [item["task_id"] for item in snapshot["items"]]
    assert ids[0] == urgent["task_id"]
    assert ids[1] == normal["task_id"]
    assert snapshot["pending"] == 3


async def test_queue_position_reported():
    service = make_service(config=base_config(queue={"capacity": 4}))
    first = await service.speak("一")
    second = await service.speak("二")
    assert first["queue_position"] == 0
    assert second["queue_position"] == 1
    assert second["queue_size"] == 2
