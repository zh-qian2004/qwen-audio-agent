"""20 ms 重分帧。

厂商输入约 64 ms（1024 samples @16 kHz）一包，不能被 20 ms 整除。
本模块用样本累积器跨包保存余数，保证样本不重复、不丢失、顺序不变，
并在算法层隐藏输入包边界。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from input.audio_frame import AudioFormat


class Rechunker:
    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        sample_width: int = 2,
        frame_ms: int = 20,
        max_buffered_bytes: Optional[int] = None,
    ):
        self.format = AudioFormat(int(sample_rate), int(channels), int(sample_width))
        self.frame_ms = int(frame_ms)
        if self.frame_ms <= 0:
            raise ValueError("frame_ms 必须为正数")
        if self.format.sample_rate <= 0 or self.format.block_align <= 0:
            raise ValueError("非法的采样率/声道/位宽")
        if (self.format.sample_rate * self.frame_ms) % 1000 != 0:
            raise ValueError(
                f"frame_ms={self.frame_ms} 无法在 {self.format.sample_rate} Hz 下整除为整数采样"
            )
        self.samples_per_frame = self.format.sample_rate * self.frame_ms // 1000
        self.frame_bytes = self.samples_per_frame * self.format.block_align
        if self.frame_bytes <= 0:
            raise ValueError("frame_bytes 计算异常")
        self.max_buffered_bytes = int(max_buffered_bytes or self.frame_bytes * 50)

        self._buffer = bytearray()
        self._lock = threading.Lock()
        self.output_frames = 0
        self.input_bytes = 0
        self.overflow_bytes = 0
        self.last_error: Optional[str] = None

    def feed(self, pcm: bytes) -> List[bytes]:
        """追加输入 PCM，返回所有已凑满的 20 ms 帧（按输入顺序）。"""
        if not pcm:
            return []
        if len(pcm) % self.format.block_align != 0:
            # 非采样对齐的尾部无法安全拼接：记错误并丢弃这一包的残余
            self.last_error = f"输入不足一个采样帧（{len(pcm)} bytes）"
            self.input_bytes += len(pcm)
            return []

        frames: List[bytes] = []
        with self._lock:
            self.input_bytes += len(pcm)
            self._buffer.extend(pcm)
            while len(self._buffer) >= self.frame_bytes:
                frames.append(bytes(self._buffer[: self.frame_bytes]))
                del self._buffer[: self.frame_bytes]
            if len(self._buffer) > self.max_buffered_bytes:
                # 只可能在输入速率异常时触发，保护内存
                self.overflow_bytes += len(self._buffer)
                self._buffer.clear()
                self.last_error = "重分帧缓冲溢出，已清空"
        self.output_frames += len(frames)
        return frames

    def flush(self) -> int:
        """停止时返回仍未成帧的字节数并清空缓冲。"""
        with self._lock:
            remaining = len(self._buffer)
            self._buffer.clear()
        return remaining

    @property
    def buffered_bytes(self) -> int:
        with self._lock:
            return len(self._buffer)

    def status(self) -> Dict[str, Any]:
        return {
            "frame_ms": self.frame_ms,
            "frame_bytes": self.frame_bytes,
            "samples_per_frame": self.samples_per_frame,
            "output_rate_hz": round(1000.0 / self.frame_ms, 1) if self.frame_ms else 0.0,
            "buffered_bytes": self.buffered_bytes,
            "output_frames": self.output_frames,
            "input_bytes": self.input_bytes,
            "overflow_bytes": self.overflow_bytes,
            "last_error": self.last_error,
        }
