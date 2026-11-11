"""音频 Processor 稳定接口。

ASR / TSE / VAD 等具体模型在后续任务中接入，本文件只定义契约：

* ``start(session)``  会话开始（可读取 session.pre_roll_ms 等信息）
* ``accept(frame)``   接收一个 20 ms AudioFrame，不应阻塞事件循环
* ``finish()``        正常结束（输入停止/会话主动停止）
* ``cancel()``        异常结束（缺口、客户端断开、服务停止）
* ``status()``        结构化状态，进入 /api/audio/status 的 consumers/sessions
"""
from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable

from input.audio_frame import AudioFrame


@runtime_checkable
class AudioProcessor(Protocol):
    name: str

    async def start(self, session: Any) -> None: ...

    async def accept(self, frame: AudioFrame) -> None: ...

    async def finish(self) -> Dict[str, Any]: ...

    async def cancel(self) -> None: ...

    def status(self) -> Dict[str, Any]: ...


class BaseProcessor:
    """可选的通用实现，便于后续 Provider 复用。"""

    name = "base"

    def __init__(self) -> None:
        self.phase = "created"
        self.frames = 0
        self.bytes = 0
        self.first_sequence = None
        self.last_sequence = None
        self.errors: list = []
        self.session = None

    async def start(self, session) -> None:
        self.session = session
        self.phase = "running"

    async def accept(self, frame: AudioFrame) -> None:
        if self.last_sequence is not None and frame.sequence != self.last_sequence + 1:
            self.errors.append(
                {
                    "reason": "sequence_gap",
                    "expected": self.last_sequence + 1,
                    "received": frame.sequence,
                }
            )
        if self.first_sequence is None:
            self.first_sequence = frame.sequence
        self.last_sequence = frame.sequence
        self.frames += 1
        self.bytes += frame.byte_length

    async def finish(self) -> Dict[str, Any]:
        self.phase = "finished"
        return self.status()

    async def cancel(self) -> None:
        self.phase = "cancelled"

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "phase": self.phase,
            "frames": self.frames,
            "bytes": self.bytes,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "errors": list(self.errors[-5:]),
        }
