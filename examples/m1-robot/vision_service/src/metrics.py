import os
import threading
import time
from pathlib import Path


def _read_number(path, cast=int):
    try:
        return cast(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def bpu_metrics(root=Path("/sys/devices/system/bpu")):
    root = Path(root)
    utilization = _read_number(root / "ratio", float)
    core = root / "bpu0"
    current_paths = list((core / "devfreq").glob("*/cur_freq"))
    max_paths = list((core / "devfreq").glob("*/max_freq"))
    users = set()
    try:
        for line in (root / "users").read_text().splitlines():
            fields = line.split()
            if fields and fields[0].isdigit():
                users.add(int(fields[0]))
    except OSError:
        pass
    current = _read_number(current_paths[0]) if current_paths else None
    maximum = _read_number(max_paths[0]) if max_paths else None
    return {
        "available": utilization is not None or current is not None,
        "utilization_percent": utilization,
        "frequency_hz": current,
        "max_frequency_hz": maximum,
        "user_count": len(users),
    }


class ResourceMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.previous = None

    def sample(self):
        now = time.monotonic()
        with self.lock:
            cpu_percent = None
            rss_kib = None
            try:
                stat = Path("/proc/self/stat").read_text().split()
                ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
                cpu_seconds = (int(stat[13]) + int(stat[14])) / ticks
                if self.previous is not None and now > self.previous[0]:
                    cpu_percent = round(
                        100.0 * (cpu_seconds - self.previous[1]) / (now - self.previous[0]), 2
                    )
                self.previous = (now, cpu_seconds)
                status = Path("/proc/self/status").read_text().splitlines()
                for line in status:
                    if line.startswith("VmRSS:"):
                        rss_kib = int(line.split()[1])
                        break
            except (OSError, ValueError, IndexError):
                pass
        return {
            "pid": os.getpid(),
            "cpu_percent": cpu_percent,
            "rss_kib": rss_kib,
            "bpu": bpu_metrics(),
        }
