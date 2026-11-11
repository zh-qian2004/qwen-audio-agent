import logging
import os
import socket
import struct
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux-only dependency
    fcntl = None


LOG = logging.getLogger("dashboard.system")


def _read_key_value(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def _ip_addresses():
    addresses = {}
    if fcntl is None:
        return addresses
    for interface in os.listdir("/sys/class/net"):
        if interface == "lo":
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            request = struct.pack("256s", interface.encode()[:15])
            response = fcntl.ioctl(sock.fileno(), 0x8915, request)
            addresses[interface] = socket.inet_ntoa(response[20:24])
        except OSError:
            continue
        finally:
            sock.close()
    return addresses


def _read_number(path, cast=int):
    try:
        return cast(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _bpu_metrics(root=Path("/sys/devices/system/bpu")):
    """Read X5 BPU telemetry from sysfs without spawning monitoring tools."""
    root = Path(root)
    core_count = _read_number(root / "core_num")
    core_paths = sorted(root.glob("bpu[0-9]*"), key=lambda path: path.name)
    cores = []
    for index, core in enumerate(core_paths):
        ratio = _read_number(core / "ratio", float)
        devfreq_paths = list((core / "devfreq").glob("*/cur_freq"))
        max_freq_paths = list((core / "devfreq").glob("*/max_freq"))
        current_hz = _read_number(devfreq_paths[0]) if devfreq_paths else None
        max_hz = _read_number(max_freq_paths[0]) if max_freq_paths else None
        if ratio is None and current_hz is None and max_hz is None:
            continue
        cores.append({
            "id": index,
            "utilization_percent": ratio,
            "frequency_hz": current_hz,
            "max_frequency_hz": max_hz,
        })

    aggregate_ratio = _read_number(root / "ratio", float)
    if aggregate_ratio is None:
        ratios = [core["utilization_percent"] for core in cores
                  if core["utilization_percent"] is not None]
        aggregate_ratio = sum(ratios) / len(ratios) if ratios else None

    users = {}
    try:
        for line in (root / "users").read_text().splitlines():
            fields = line.split()
            if len(fields) < 2 or not fields[0].isdigit():
                continue
            try:
                pid, ratio = int(fields[0]), float(fields[1])
            except ValueError:
                continue
            users[pid] = max(users.get(pid, 0.0), ratio)
    except OSError:
        pass

    primary = cores[0] if cores else {}
    available = aggregate_ratio is not None or bool(cores)
    return {
        "available": available,
        "core_count": core_count if core_count is not None else len(cores),
        "utilization_percent": round(aggregate_ratio, 1) if aggregate_ratio is not None else None,
        "frequency_hz": primary.get("frequency_hz"),
        "max_frequency_hz": primary.get("max_frequency_hz"),
        "user_count": len(users),
        "users": [{"pid": pid, "utilization_percent": ratio}
                  for pid, ratio in sorted(users.items())],
        "cores": cores,
    }


class SystemMonitor:
    def __init__(self, store):
        self.store = store
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="system-monitor", daemon=True)
        self.previous_cpu = None
        self.previous_process = None

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    @staticmethod
    def _cpu_times():
        values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    def _cpu_percent(self):
        current = self._cpu_times()
        previous = self.previous_cpu
        self.previous_cpu = current
        if previous is None:
            return None
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        return round(100.0 * (total_delta - idle_delta) / total_delta, 1) if total_delta > 0 else 0.0

    def _process_usage(self, now):
        stat = Path("/proc/self/stat").read_text().split()
        ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        cpu_seconds = (int(stat[13]) + int(stat[14])) / ticks
        previous = self.previous_process
        self.previous_process = (now, cpu_seconds)
        cpu_percent = None
        if previous is not None and now > previous[0]:
            cpu_percent = round(100.0 * (cpu_seconds - previous[1]) / (now - previous[0]), 2)
        status = _read_key_value("/proc/self/status")
        rss_kib = int(status.get("VmRSS", "0 kB").split()[0])
        return {"cpu_percent": cpu_percent, "rss_kib": rss_kib, "pid": os.getpid()}

    @staticmethod
    def _temperatures():
        result = {}
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                name = (zone / "type").read_text().strip()
                result[name] = round(int((zone / "temp").read_text().strip()) / 1000.0, 1)
            except (OSError, ValueError):
                continue
        return result

    def sample(self):
        now = time.monotonic()
        memory = _read_key_value("/proc/meminfo")
        total_kib = int(memory["MemTotal"].split()[0])
        available_kib = int(memory["MemAvailable"].split()[0])
        disk = os.statvfs("/userdata")
        disk_total = disk.f_blocks * disk.f_frsize
        disk_available = disk.f_bavail * disk.f_frsize
        root_flags = ""
        for line in Path("/proc/mounts").read_text().splitlines():
            fields = line.split()
            if len(fields) > 3 and fields[1] == "/":
                root_flags = fields[3]
                break
        try:
            firmware = Path("/etc/version").read_text().strip()
        except OSError:
            firmware = "unknown"
        return {
            "hostname": socket.gethostname(),
            "firmware": firmware,
            "ip_addresses": _ip_addresses(),
            "uptime_seconds": float(Path("/proc/uptime").read_text().split()[0]),
            "load_average": list(os.getloadavg()),
            "cpu_percent": self._cpu_percent(),
            "bpu": _bpu_metrics(),
            "memory": {
                "total_kib": total_kib,
                "used_kib": total_kib - available_kib,
                "used_percent": round(100.0 * (total_kib - available_kib) / total_kib, 1),
            },
            "disk": {
                "path": "/userdata",
                "total_bytes": disk_total,
                "used_bytes": disk_total - disk_available,
                "used_percent": round(100.0 * (disk_total - disk_available) / disk_total, 1),
            },
            "temperatures_c": self._temperatures(),
            "root_mount": "ro" if "ro" in root_flags.split(",") else root_flags,
            "service": self._process_usage(now),
        }

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.store.update_system(self.sample())
            except Exception:
                LOG.exception("System metrics sampling failed")
            self.stop_event.wait(1.0)
