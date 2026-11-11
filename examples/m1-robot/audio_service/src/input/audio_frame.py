"""统一、不可变的内部音频帧类型。

ROS 侧只负责把 sensor_msgs/Image 解析成 ``AudioFrame`` 并写入入口队列；
后续的重分帧、缓存、总线分发都基于相同的帧结构，不再感知 ROS 消息。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_ENCODING_RE = re.compile(r"^pcm_(\d+)_(\d+)_(\d+)$")
SUPPORTED_SAMPLE_WIDTH = 2


@dataclass(frozen=True)
class AudioFormat:
    """PCM 布局描述（不含数据）。"""

    sample_rate: int
    channels: int
    sample_width: int

    @property
    def block_align(self) -> int:
        """一个采样帧（所有声道）占用的字节数。"""
        return self.channels * self.sample_width

    @property
    def encoding(self) -> str:
        return f"pcm_{self.sample_rate}_{self.sample_width * 8}_{self.channels}"

    def validate_payload(self, pcm: bytes, require_samples: bool = True) -> None:
        if self.sample_rate <= 0 or self.channels <= 0 or self.sample_width <= 0:
            raise ValueError("非法的音频格式：采样率/声道/位宽必须为正数")
        if self.sample_width != SUPPORTED_SAMPLE_WIDTH:
            raise ValueError(f"仅支持 16-bit PCM，当前 {self.sample_width * 8}-bit")
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) == 0:
            raise ValueError("PCM 数据为空")
        if len(pcm) % self.block_align != 0:
            raise ValueError(
                f"PCM 字节数 {len(pcm)} 不是采样帧对齐（block_align={self.block_align}）"
            )
        if require_samples and len(pcm) < self.block_align:
            raise ValueError("PCM 数据不足一个采样帧")


def parse_encoding(encoding: Optional[str]) -> Optional[AudioFormat]:
    """解析厂商 ``pcm_<rate>_<bits>_<channels>`` 编码串。"""
    match = _ENCODING_RE.match(str(encoding or "").strip().lower())
    if not match:
        return None
    rate, bits, channels = (int(value) for value in match.groups())
    if bits % 8 != 0:
        return None
    return AudioFormat(sample_rate=rate, channels=channels, sample_width=bits // 8)


@dataclass(frozen=True)
class AudioFrame:
    """Gateway 内部帧。

    * ``sequence``：Gateway 单调递增序号（重分帧后的 20 ms 帧计数）
    * ``captured_at_ns``：本机单调时钟（time.monotonic_ns），用于延迟/年龄计算
    * ``source_timestamp_ns``：ROS header.stamp（未提供时为 None）
    * ``input_sequence``：入口 ROS 包序号，用于连续性诊断
    * ``discontinuity``：入口包时间戳/序号出现不连续时为 True
    """

    sequence: int
    captured_at_ns: int
    source_timestamp_ns: Optional[int]
    sample_rate: int
    channels: int
    sample_width: int
    encoding: str
    pcm: bytes
    input_sequence: int = 0
    discontinuity: bool = False

    @property
    def block_align(self) -> int:
        return self.channels * self.sample_width

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // self.block_align if self.block_align else 0

    @property
    def byte_length(self) -> int:
        return len(self.pcm)

    @property
    def duration_ms(self) -> float:
        if not self.sample_rate:
            return 0.0
        return self.sample_count / self.sample_rate * 1000.0

    @property
    def format(self) -> AudioFormat:
        return AudioFormat(self.sample_rate, self.channels, self.sample_width)

    def continuity(self) -> dict:
        return {
            "sequence": self.sequence,
            "input_sequence": self.input_sequence,
            "discontinuity": self.discontinuity,
        }


def make_frame(
    *,
    sequence: int,
    pcm: bytes,
    fmt: AudioFormat,
    captured_at_ns: int,
    source_timestamp_ns: Optional[int] = None,
    input_sequence: int = 0,
    discontinuity: bool = False,
) -> AudioFrame:
    return AudioFrame(
        sequence=int(sequence),
        captured_at_ns=int(captured_at_ns),
        source_timestamp_ns=source_timestamp_ns,
        sample_rate=fmt.sample_rate,
        channels=fmt.channels,
        sample_width=fmt.sample_width,
        encoding=fmt.encoding,
        pcm=bytes(pcm),
        input_sequence=int(input_sequence),
        discontinuity=discontinuity,
    )
