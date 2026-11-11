"""MP3 -> 48 kHz / 16-bit / 双声道 raw PCM 转换与格式校验。

机器人端 ffmpeg 4.4.2 已内置 mp3 解码器，ARM64 下稳定，无需额外编译。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from state import ServiceError

LOG = logging.getLogger(__name__)


@dataclass
class PcmAudio:
    data: bytes
    sample_rate: int
    channels: int
    sample_width: int
    source: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.sample_rate != 48000:
            raise ServiceError("pcm_format_invalid", f"unexpected sample rate: {self.sample_rate}", 500)
        if self.sample_width != 2:
            raise ServiceError("pcm_format_invalid", f"unexpected sample width: {self.sample_width}", 500)
        if self.channels != 2:
            raise ServiceError("pcm_format_invalid", f"unexpected channels: {self.channels}", 500)
        if not self.data:
            raise ServiceError("decode_failed", "PCM payload is empty", 502)
        if len(self.data) % self.frame_bytes != 0:
            raise ServiceError(
                "pcm_format_invalid",
                f"PCM length {len(self.data)} is not aligned to frame size {self.frame_bytes}",
                500,
            )

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width

    @property
    def frames(self) -> int:
        return len(self.data) // self.frame_bytes

    @property
    def duration_seconds(self) -> float:
        return self.frames / float(self.sample_rate)

    def format_dict(self) -> Dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "sample_format": "s16le",
            "channels": self.channels,
            "bytes": len(self.data),
            "frames": self.frames,
            "frame_bytes": self.frame_bytes,
            "duration_seconds": round(self.duration_seconds, 3),
            "encoding": "pcm_48000_16_2",
        }

    def chunks(self, chunk_ms: float) -> List[bytes]:
        """按 chunk_ms 切块；最后一包保持整帧对齐（必要时补零，< 1 帧）。"""
        size = max(self.frame_bytes, int(self.sample_rate * chunk_ms / 1000.0) * self.frame_bytes)
        out: List[bytes] = []
        for offset in range(0, len(self.data), size):
            piece = self.data[offset : offset + size]
            if len(piece) % self.frame_bytes:
                piece = piece + b"\x00" * (self.frame_bytes - len(piece) % self.frame_bytes)
            out.append(piece)
        return out


class AudioConverter:
    def __init__(self, config: Dict[str, Any]):
        self.sample_rate = int(config.get("sample_rate", 48000))
        self.channels = int(config.get("channels", 2))
        self.sample_width = int(config.get("sample_width", 2))
        self.chunk_ms = float(config.get("chunk_ms", 60))
        self.ffmpeg = str(config.get("ffmpeg_path", "ffmpeg"))
        self.ffprobe = str(config.get("ffprobe_path", "ffprobe"))
        self.decode_timeout = float(config.get("decode_timeout_seconds", 30))
        # ROS 的 /opt/ros/humble/lib 会引入与系统 ffmpeg 不兼容的库搜索顺序
        # （实机表现为 libXrandr.so.2 not found），因此子进程使用干净环境。
        self.clean_child_env = bool(config.get("clean_child_env", True))

    def _child_env(self) -> Optional[Dict[str, str]]:
        if not self.clean_child_env:
            return None
        env = dict(os.environ)
        env.pop("LD_LIBRARY_PATH", None)
        env.pop("LD_PRELOAD", None)
        return env

    @property
    def chunk_bytes(self) -> int:
        return int(self.sample_rate * self.chunk_ms / 1000.0) * self.channels * self.sample_width

    def probe(self, payload: bytes) -> Dict[str, Any]:
        """尽力探测源音频参数，失败不影响主流程。"""
        cmd = [
            self.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration,bit_rate",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            "-i",
            "pipe:0",
        ]
        try:
            result = subprocess.run(
                cmd, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, env=self._child_env(),
            )
        except Exception as exc:  # pragma: no cover - 机器上 ffprobe 缺失时
            LOG.warning("ffprobe 探测失败：%s", exc)
            return {}
        if result.returncode != 0:
            LOG.warning("ffprobe 返回 %s", result.returncode)
            return {}
        try:
            info = json.loads(result.stdout.decode("utf-8", "ignore") or "{}")
        except json.JSONDecodeError:
            return {}
        stream = (info.get("streams") or [{}])[0]
        fmt = info.get("format") or {}
        return {
            "format_name": fmt.get("format_name"),
            "duration_seconds": _to_float(fmt.get("duration")),
            "bit_rate": _to_int(fmt.get("bit_rate")),
            "codec_name": stream.get("codec_name"),
            "source_sample_rate": _to_int(stream.get("sample_rate")),
            "source_channels": _to_int(stream.get("channels")),
        }

    def decode(self, payload: bytes) -> PcmAudio:
        if not payload:
            raise ServiceError("decode_failed", "输入音频为空", 502)
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            "pipe:0",
            "-vn",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(self.sample_rate),
            "-ac",
            str(self.channels),
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                cmd,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.decode_timeout,
                env=self._child_env(),
            )
        except subprocess.TimeoutExpired:
            raise ServiceError("decode_timeout", f"ffmpeg 解码超时（>{self.decode_timeout}s）", 504) from None
        except FileNotFoundError:
            raise ServiceError("decode_unavailable", f"找不到 ffmpeg：{self.ffmpeg}", 503) from None
        except OSError as exc:
            raise ServiceError("decode_failed", f"ffmpeg 执行失败：{exc}", 502) from None

        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            tail = " | ".join(stderr[-3:])[:400] or f"ffmpeg exit {result.returncode}"
            raise ServiceError("decode_failed", f"MP3 解码失败：{tail}", 502, {"ffmpeg_exit": result.returncode})
        if not result.stdout:
            raise ServiceError("decode_failed", "ffmpeg 未输出 PCM 数据", 502)

        pcm = PcmAudio(result.stdout, self.sample_rate, self.channels, self.sample_width)
        pcm.validate()
        LOG.info(
            "PCM 转换完成 %d bytes / %.3fs / chunk=%d bytes",
            len(pcm.data),
            pcm.duration_seconds,
            self.chunk_bytes,
        )
        return pcm


def _to_float(value) -> Optional[float]:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
