import json
import subprocess
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from speaker_volume import SpeakerVolume, VolumeError  # noqa: E402


class FakeAmixer:
    def __init__(self, percent=20):
        self.percent = percent
        self.db_gain = 0.0
        self.calls = []
        self.fail = False

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "amixer: Mixer attach error")
        if "sget" in argv:
            raw = round(self.percent * 207 / 100)
            stdout = (
                f"Simple mixer control '{argv[-1]}',0\n"
                "  Capabilities: pvolume pvolume-joined\n"
                f"  Mono: Playback {raw} [{self.percent}%] [{self.db_gain:.2f}dB]\n"
            )
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        if "sset" in argv:
            if argv[-1].endswith("dB"):
                self.db_gain = float(argv[-1][:-2])
            else:
                self.percent = int(argv[-1].rstrip("%"))
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected argv: {argv}")


def make_volume(runner=None, **overrides):
    config = {
        "card": "duplexaudio",
        "min_percent": 0,
        "max_percent": 100,
        "amixer_path": sys.executable,
        "timeout_seconds": 3,
        "cache_ttl_seconds": 0,
    }
    config.update(overrides)
    volume = SpeakerVolume(config, runner=runner or FakeAmixer())
    volume._card_index = lambda: 2
    return volume


class SpeakerVolumeTests(unittest.TestCase):
    def test_status_reads_percent(self):
        runner = FakeAmixer(percent=20)
        volume = make_volume(runner)
        status = volume.status()
        self.assertTrue(status["supported"])
        self.assertEqual(status["percent"], 20)
        self.assertFalse(status["muted"])
        self.assertEqual(status["card_index"], 2)
        self.assertEqual(status["controls"], ["DAC"])
        self.assertEqual(status["scale"], "perceptual")
        self.assertEqual(status["db_gain"], 0.0)
        self.assertEqual(runner.calls[0], [sys.executable, "-M", "-c", "2", "sget", "DAC"])
        self.assertFalse(any("sset" in call for call in runner.calls))

    def test_shipped_config_targets_vendor_playback_control(self):
        config = json.loads((Path(__file__).resolve().parent.parent /
                             "config" / "dashboard.json").read_text(encoding="utf-8"))
        self.assertEqual(config["speaker_volume"]["controls"], ["DAC"])

    def test_set_percent_updates_dac_only(self):
        runner = FakeAmixer()
        status = make_volume(runner).set_percent(25)
        self.assertEqual(status["percent"], 25)
        self.assertEqual([call[-2:] for call in runner.calls if "sset" in call],
                         [["DAC", "25%"]])
        self.assertIn("-M", next(call for call in runner.calls if "sset" in call))

    def test_mapped_read_reports_perceptual_percent_and_real_gain(self):
        def runner(argv, **kwargs):
            percent = 2 if "-M" in argv else 20
            return subprocess.CompletedProcess(argv, 0,
                "Limits: Playback 0 - 207\n"
                f"Mono: Playback 41 [{percent}%] [-75.00dB]\n", "")

        status = make_volume(runner).status()
        self.assertEqual(status["percent"], 2)
        self.assertEqual(status["db_gain"], -75.0)
        self.assertFalse(status["muted"])

    def test_direct_db_gain_sets_negative_value_and_reads_back(self):
        runner = FakeAmixer()
        status = make_volume(runner).set_db_gain(-32)
        self.assertEqual(status["db_gain"], -32.0)
        self.assertEqual(status["db_min"], -95.5)
        self.assertEqual(status["db_max"], 8.0)
        self.assertEqual(status["db_step"], 0.5)
        sets = [call for call in runner.calls if "sset" in call]
        self.assertEqual(sets[0][-4:], ["--", "sset", "DAC", "-32dB"])

    def test_invalid_db_gain_never_writes_hardware(self):
        runner = FakeAmixer()
        volume = make_volume(runner)
        for value in (None, "loud", float("nan"), float("inf"), -95.6, 8.1):
            with self.subTest(value=value), self.assertRaises(VolumeError) as context:
                volume.set_db_gain(value)
            self.assertEqual(context.exception.code, "invalid_db_gain")
        self.assertEqual(runner.calls, [])

    def test_db_write_readback_mismatch_is_rejected(self):
        runner = FakeAmixer()

        def ignored_write(argv, **kwargs):
            if "sset" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return runner(argv, **kwargs)

        with self.assertRaises(VolumeError) as context:
            make_volume(ignored_write).set_db_gain(-32)
        self.assertEqual(context.exception.code, "verify_failed")

    def test_rounded_mapped_zero_is_not_reported_as_muted(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0,
                "Limits: Playback 0 - 207\n"
                "Mono: Playback 5 [0%] [-93.00dB]\n", "")

        status = make_volume(runner).status()
        self.assertEqual(status["percent"], 0)
        self.assertFalse(status["muted"])

    def test_set_percent_updates_both_controls(self):
        runner = FakeAmixer()
        volume = make_volume(runner, controls=["SPKL", "SPKR"])
        status = volume.set_percent(35)
        self.assertTrue(status["supported"])
        self.assertEqual(status["percent"], 35)
        sets = [call for call in runner.calls if "sset" in call]
        self.assertEqual(len(sets), 2)
        self.assertEqual(sets[0][-2:], ["SPKL", "35%"])
        self.assertEqual(sets[1][-2:], ["SPKR", "35%"])

    def test_set_percent_clamps(self):
        runner = FakeAmixer()
        volume = make_volume(runner)
        self.assertEqual(volume.set_percent(150)["percent"], 100)
        muted = volume.set_percent(-10)
        self.assertEqual(muted["percent"], 0)
        self.assertTrue(muted["muted"])

    def test_invalid_percent_rejected(self):
        volume = make_volume()
        for value in ("loud", None, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(VolumeError) as context:
                volume.set_percent(value)
            self.assertEqual(context.exception.code, "invalid_percent")

    def test_write_without_hardware_change_rejected(self):
        runner = FakeAmixer(percent=20)

        def ignored_write(argv, **kwargs):
            if "sset" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return runner(argv, **kwargs)

        with self.assertRaises(VolumeError) as context:
            make_volume(ignored_write).set_percent(35)
        self.assertEqual(context.exception.code, "verify_failed")

    def test_failed_readback_is_not_success(self):
        runner = FakeAmixer()

        def failed_readback(argv, **kwargs):
            result = runner(argv, **kwargs)
            if "sset" in argv:
                runner.fail = True
            return result

        with self.assertRaises(VolumeError) as context:
            make_volume(failed_readback).set_percent(35)
        self.assertEqual(context.exception.code, "read_failed")

    def test_failed_write_invalidates_cached_status(self):
        runner = FakeAmixer()
        volume = make_volume(runner, cache_ttl_seconds=60)
        self.assertTrue(volume.status()["supported"])
        runner.fail = True
        with self.assertRaises(VolumeError):
            volume.set_percent(35)
        self.assertFalse(volume.status()["supported"])

    def test_read_failure_reported(self):
        runner = FakeAmixer()
        runner.fail = True
        volume = make_volume(runner)
        status = volume.status()
        self.assertFalse(status["supported"])
        self.assertEqual(status["error"]["code"], "read_failed")

    def test_set_failure_reported(self):
        runner = FakeAmixer()
        volume = make_volume(runner)
        runner.fail = True
        with self.assertRaises(VolumeError) as context:
            volume.set_percent(50)
        self.assertEqual(context.exception.code, "set_failed")
        self.assertEqual(context.exception.status_code, 502)

    def test_card_missing(self):
        volume = make_volume()
        volume._card_index = lambda: None
        status = volume.status()
        self.assertFalse(status["supported"])
        self.assertEqual(status["error"]["code"], "card_missing")

    def test_amixer_missing(self):
        volume = make_volume(amixer_path="/nonexistent/amixer")
        status = volume.status()
        self.assertFalse(status["supported"])
        self.assertEqual(status["error"]["code"], "amixer_missing")


if __name__ == "__main__":
    unittest.main()
