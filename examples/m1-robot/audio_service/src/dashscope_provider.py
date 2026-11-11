"""DashScope TTS Provider。

只负责“文本 -> MP3 字节”，不做解码与播放。
任何外部异常都被转换成稳定错误码，日志与 API 响应中不会出现完整 API Key。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from state import ServiceError, mask_key, redact_secret

LOG = logging.getLogger(__name__)


class DashScopeProvider:
    def __init__(self, config: Dict[str, Any], api_key: Optional[str] = None, synthesizer_factory: Optional[Callable] = None):
        self.model = str(config.get("model", "qwen-audio-3.1-tts-flash"))
        self.voice = str(config.get("voice", "longanhuan_v3.1"))
        self.base_url = str(config.get("base_websocket_api_url", ""))
        self.api_key_env = str(config.get("api_key_env", "DASHSCOPE_API_KEY"))
        self._api_key = api_key if api_key is not None else os.environ.get(self.api_key_env)
        self._synth_factory = synthesizer_factory
        self._sdk_error: Optional[str] = None
        if self._synth_factory is None:
            try:
                import dashscope  # noqa: F401
            except Exception as exc:  # pragma: no cover - 依赖缺失时降级
                self._sdk_error = redact_secret(exc)
                LOG.error("dashscope SDK 不可用：%s", self._sdk_error)

        self._lock = threading.Lock()
        self.requests = 0
        self.successes = 0
        self.failures = 0
        self.last_success_at: Optional[float] = None
        self.last_error: Optional[Dict[str, Any]] = None
        self.last_latency_ms: Optional[float] = None
        self.last_audio_bytes: Optional[int] = None

    # ---- 状态 -----------------------------------------------------
    @property
    def api_key(self) -> Optional[str]:
        return self._api_key

    @property
    def available(self) -> bool:
        return bool(self._api_key) and self._sdk_error is None

    def unavailable_reason(self) -> Optional[str]:
        if self._sdk_error:
            return f"dashscope SDK import failed: {self._sdk_error}"
        if not self._api_key:
            return f"{self.api_key_env} is not configured"
        return None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "available": self.available,
                "unavailable_reason": self.unavailable_reason(),
                "model": self.model,
                "voice": self.voice,
                "endpoint": self.base_url,
                "api_key_env": self.api_key_env,
                "api_key_configured": bool(self._api_key),
                "api_key_masked": mask_key(self._api_key),
                "requests": self.requests,
                "successes": self.successes,
                "failures": self.failures,
                "last_success_at": round(self.last_success_at, 3) if self.last_success_at else None,
                "last_latency_ms": round(self.last_latency_ms, 1) if self.last_latency_ms is not None else None,
                "last_audio_bytes": self.last_audio_bytes,
                "last_error": dict(self.last_error) if self.last_error else None,
            }

    # ---- 合成 -----------------------------------------------------
    def _build_synthesizer(self, voice: str, speech_rate: float):
        kwargs = {
            "model": self.model,
            "voice": voice,
            "speech_rate": speech_rate,
        }
        if self._synth_factory is not None:
            return self._synth_factory(**kwargs)
        import dashscope
        from dashscope.audio.tts_v2 import SpeechSynthesizer

        if self.base_url:
            dashscope.base_websocket_api_url = self.base_url
        dashscope.api_key = self._api_key
        return SpeechSynthesizer(**kwargs)

    def _record_failure(self, code: str, message: str, detail: Optional[dict] = None) -> None:
        with self._lock:
            self.failures += 1
            self.last_error = {
                "code": code,
                "message": redact_secret(message),
                "detail": detail or {},
                "at": round(time.time(), 3),
            }

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        emotion: str = "neutral",
        speech_rate: float = 1.0,
    ) -> Dict[str, Any]:
        """阻塞调用，由上层放到线程池执行。情绪使用 Qwen-Audio-TTS 官方文本标签。"""
        if not self.available:
            raise ServiceError("provider_unavailable", self.unavailable_reason() or "provider unavailable", 503)

        selected_voice = voice or self.voice
        selected_emotion = emotion or "neutral"
        synthesis_text = text if selected_emotion == "neutral" else f"[{selected_emotion}]{text}"

        with self._lock:
            self.requests += 1

        started = time.monotonic()
        try:
            synthesizer = self._build_synthesizer(selected_voice, speech_rate)
            audio = synthesizer.call(synthesis_text)
        except ServiceError:
            raise
        except Exception as exc:  # 网络、鉴权、服务端错误
            message = redact_secret(exc)
            self._record_failure("provider_error", message, {"model": self.model, "voice": selected_voice})
            raise ServiceError(
                "provider_error",
                f"DashScope 合成失败：{message}",
                502,
                {"model": self.model, "voice": selected_voice},
            ) from None

        latency_ms = (time.monotonic() - started) * 1000.0
        if not audio:
            self._record_failure("provider_error", "DashScope 返回空音频", {"model": self.model})
            raise ServiceError("provider_error", "DashScope 返回空音频", 502, {"model": self.model})

        payload = audio if isinstance(audio, (bytes, bytearray)) else bytes(audio)
        with self._lock:
            self.successes += 1
            self.last_success_at = time.time()
            self.last_latency_ms = latency_ms
            self.last_audio_bytes = len(payload)
        LOG.info(
            "DashScope 合成成功 model=%s voice=%s emotion=%s speech_rate=%.2f bytes=%d latency=%.0fms",
            self.model,
            selected_voice,
            selected_emotion,
            speech_rate,
            len(payload),
            latency_ms,
        )
        return {
            "data": payload,
            "bytes": len(payload),
            "model": self.model,
            "voice": selected_voice,
            "emotion": selected_emotion,
            "speech_rate": speech_rate,
            "latency_ms": round(latency_ms, 1),
        }
