import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time


LOG = logging.getLogger("dashboard.volume")


class VolumeError(Exception):
    def __init__(self, code, message, status_code=400, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail

    def as_dict(self):
        payload = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class SpeakerVolume:
    """Read and set the vendor playback DAC through amixer.

    The active ES8326 speaker route uses HPL, aliased by DAC. SPKL/SPKR
    exist but do not control that route with the vendor mixer configuration.
    Use ALSA's mapped scale for both reads and writes: the raw register's
    20% is -75 dB on this codec and is unsuitable as a user volume scale.
    """

    def __init__(self, config, runner=None):
        self.card_name = str(config.get("card", "duplexaudio"))
        self.controls = list(config.get("controls") or ["DAC"])
        self.min_percent = int(config.get("min_percent", 0))
        self.max_percent = int(config.get("max_percent", 100))
        self.min_db_gain = float(config.get("min_db_gain", -95.5))
        self.max_db_gain = float(config.get("max_db_gain", 8.0))
        self.db_step = float(config.get("db_step", 0.5))
        self.amixer_path = (config.get("amixer_path")
                            or shutil.which("amixer") or "/usr/bin/amixer")
        self.timeout = float(config.get("timeout_seconds", 3))
        self.cache_ttl = float(config.get("cache_ttl_seconds", 1.0))
        self.runner = runner or subprocess.run
        self.lock = threading.RLock()
        self.cached = None
        self.cached_at = 0.0

    def status(self, refresh=False):
        with self.lock:
            now = time.monotonic()
            if not refresh and self.cached and now - self.cached_at < self.cache_ttl:
                return dict(self.cached)
            card_index = self._card_index()
            payload = {
                "supported": False,
                "card": self.card_name,
                "card_index": card_index,
                "controls": list(self.controls),
                "percent": None,
                "scale": "perceptual",
                "db_gain": None,
                "db_min": self.min_db_gain,
                "db_max": self.max_db_gain,
                "db_step": self.db_step,
                "min": self.min_percent,
                "max": self.max_percent,
                "muted": False,
                "error": None,
                "updated_at": time.time(),
            }
            if card_index is None:
                payload["error"] = {"code": "card_missing",
                                    "message": f"未找到音频卡 {self.card_name}"}
            elif not os.path.isfile(self.amixer_path):
                payload["error"] = {"code": "amixer_missing",
                                    "message": "amixer 不可用"}
            else:
                try:
                    payload.update(self._read_level(card_index))
                    payload["supported"] = True
                except VolumeError as error:
                    payload["error"] = error.as_dict()
            self.cached = payload
            self.cached_at = now
            return dict(payload)

    def set_percent(self, percent):
        try:
            value = int(round(float(percent)))
        except (TypeError, ValueError, OverflowError):
            raise VolumeError("invalid_percent", "音量必须是 0~100 的整数")
        value = max(self.min_percent, min(self.max_percent, value))
        return self._set_level(f"{value}%", "percent", value, 1)

    def set_db_gain(self, db_gain):
        try:
            value = float(db_gain)
        except (TypeError, ValueError, OverflowError):
            raise VolumeError("invalid_db_gain", "输出增益必须是有限的 dB 数值")
        if not math.isfinite(value):
            raise VolumeError("invalid_db_gain", "输出增益必须是有限的 dB 数值")
        if not self.min_db_gain <= value <= self.max_db_gain:
            raise VolumeError("invalid_db_gain",
                              f"输出增益需在 {self.min_db_gain:g}~{self.max_db_gain:g} dB 之间")
        return self._set_level(f"{value:g}dB", "db_gain", value, self.db_step / 2 + 0.01)

    def _set_level(self, setting, field, expected, tolerance):
        with self.lock:
            self.cached = None
            self.cached_at = 0.0
            card_index = self._card_index()
            if card_index is None:
                raise VolumeError("card_missing",
                                  f"未找到音频卡 {self.card_name}", 503)
            if not os.path.isfile(self.amixer_path):
                raise VolumeError("amixer_missing", "amixer 不可用", 503)
            for control in self.controls:
                result = self._run([
                    self.amixer_path, "-M", "-c", str(card_index),
                    "--", "sset", control, setting,
                ])
                if result.returncode != 0:
                    raise VolumeError(
                        "set_failed", f"设置 {control} 失败", 502,
                        (result.stderr or "").strip()[-300:] or None)
            status = self.status(refresh=True)
            if not status["supported"]:
                raise VolumeError("read_failed", "设置后无法读取扬声器音量", 502,
                                  status["error"]["message"])
            actual = status[field]
            if actual is None or abs(actual - expected) > tolerance:
                raise VolumeError("verify_failed", "扬声器音量写入后校验失败", 502,
                                  f"请求 {setting}，实际 {actual}")
            LOG.info("Speaker volume set to %s via %s", setting,
                     "/".join(self.controls))
            return status

    def _read_level(self, card_index):
        result = self._run([
            self.amixer_path, "-M", "-c", str(card_index),
            "sget", self.controls[0],
        ])
        if result.returncode != 0:
            raise VolumeError("read_failed", "读取扬声器音量失败", 502,
                              (result.stderr or "").strip()[-300:] or None)
        output = result.stdout or ""
        match = re.search(r"\[(\d+)%\]", output)
        if not match:
            raise VolumeError("read_failed", "无法解析 amixer 输出", 502,
                              (result.stdout or "").strip()[-300:] or None)
        percent = int(match.group(1))
        db = re.search(r"\[(-?\d+(?:\.\d+)?)dB\]", output)
        limits = re.search(r"Limits: Playback (\d+) - (\d+)", output)
        raw = re.search(r": Playback (\d+) \[", output)
        # Mapped percentages can round to zero before the control reaches
        # its actual minimum; keep that distinct from setting volume to zero.
        muted = int(raw.group(1)) == int(limits.group(1)) if raw and limits else percent == 0
        return {"percent": percent, "db_gain": float(db.group(1)) if db else None,
                "muted": muted}

    def _run(self, argv):
        try:
            return self.runner(argv, capture_output=True, text=True,
                               timeout=self.timeout)
        except FileNotFoundError:
            raise VolumeError("amixer_missing", "amixer 未安装", 503)
        except subprocess.TimeoutExpired:
            raise VolumeError("timeout", "amixer 执行超时", 504)
        except OSError as error:
            raise VolumeError("set_failed", "无法启动 amixer", 502, str(error))

    def _card_index(self):
        try:
            with open("/proc/asound/cards", "r", encoding="utf-8") as handle:
                for line in handle:
                    match = re.match(r"\s*(\d+)\s+\[([^\]]+)\]", line)
                    if match and match.group(2).strip().split()[0] == self.card_name:
                        return int(match.group(1))
        except OSError:
            return None
        return None
