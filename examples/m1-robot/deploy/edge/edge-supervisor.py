"""Boot-time Agent supervision using only the robot's Python standard library."""
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

BASE = Path('/userdata/developer/audio-agent')
STOP = threading.Event()
LOG = logging.getLogger('edge-supervisor')


def fetch(port, path='/api/health'):
    with urlopen('http://127.0.0.1:%s%s' % (port, path), timeout=3) as response:
        return json.load(response)


def worker_alive():
    try:
        pid = int((BASE / 'run/agent.pid').read_text().strip())
        command = Path('/proc/%d/cmdline' % pid).read_bytes().replace(b'\0', b' ')
        return b'node examples/m1-robot/start.mjs' in command or str(BASE / 'scripts/edge-run.sh').encode() in command
    except (OSError, ValueError):
        return False


def cleanup_worker():
    """Clean our previous launcher's isolated process group, including orphans."""
    try:
        if (BASE / 'run/agent.boot-id').read_text() != Path('/proc/sys/kernel/random/boot_id').read_text():
            return  # PID/group numbers from an earlier boot cannot be trusted.
        group = int((BASE / 'run/agent.pid').read_text())
    except (OSError, ValueError):
        return
    if group <= 1:
        return
    def members():
        found = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                if os.getpgid(pid) != group:
                    continue
                command = (entry / 'cmdline').read_bytes()
                if not command:  # Exited/zombie processes need no signal.
                    continue
                cwd = os.readlink(entry / 'cwd')
                if not (cwd.startswith(str(BASE) + '/') or str(BASE).encode() in command):
                    raise RuntimeError('refusing cleanup of an unrecognized process group')
                found.append(pid)
            except (FileNotFoundError, ProcessLookupError):
                continue
        return found
    if not members():
        return
    os.killpg(group, signal.SIGTERM)
    for _ in range(20):
        if not members():
            return
        time.sleep(0.5)
    if members():
        os.killpg(group, signal.SIGKILL)


def dependencies():
    for port in (8091, 8093, 8080):
        health = fetch(port)
        if health.get('status') not in ('ok', 'ready', 'healthy'):
            raise RuntimeError('dependency %d not healthy' % port)
        if port == 8093 and not health.get('input', {}).get('online'):
            raise RuntimeError('microphone offline')


def healthy():
    gateway = fetch(18889)
    backend = gateway.get('backend', {})
    if gateway.get('status') != 'ready' or backend.get('ok') is not True or backend.get('status') != 'ready':
        raise RuntimeError('Gateway/backend not ready')
    audio = fetch(8093).get('input', {})
    if not audio.get('online') or not any(str(c.get('name', '')).startswith('ws-input-') and not c.get('closed') for c in audio.get('consumer_details', [])):
        raise RuntimeError('Connector microphone stream offline')


def control(action):
    # Retain the existing launcher's graceful shutdown and active-release lookup.
    with (BASE / 'logs/agent.log').open('a') as log:
        result = subprocess.run(['bash', str(BASE / 'scripts/edge-service.sh'), action],
                                stdout=log, stderr=log, timeout=190, close_fds=True)
    if result.returncode:
        raise RuntimeError('%s exited %d' % (action, result.returncode))


def publish(state, reason=''):
    content = dict(pid=os.getpid(), state=state, reason=reason, updated_at=time.time())
    path = BASE / 'run/supervisor-state.json'
    temp = path.with_suffix('.next')
    temp.write_text(json.dumps(content))
    temp.replace(path)


def main():
    for name in ('run', 'logs'):
        (BASE / name).mkdir(parents=True, exist_ok=True)
    lock = (BASE / 'run/supervisor.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    handler = RotatingFileHandler(BASE / 'logs/supervisor.log', maxBytes=1024*1024, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    LOG.info('supervisor started pid=%d', os.getpid())
    failures = 0
    backoff = 5
    previous = None
    try:
        while not STOP.is_set():
            delay = 15
            state, reason = 'healthy', ''
            try:
                if (BASE / 'run/manual-stop').exists():
                    state = 'paused'
                    failures = 0
                elif not worker_alive():
                    dependencies()
                    # TCP reachability also checks DNS; no credentials or model requests.
                    with socket.create_connection(('dashscope.aliyuncs.com', 443), timeout=5):
                        pass
                    publish('starting')
                    control('start-worker')
                    healthy()
                    failures, backoff = 0, 5
                else:
                    try:
                        healthy()
                        failures, backoff = 0, 5
                    except Exception:
                        failures += 1
                        if failures < 4:
                            raise
                        # Four failed checks avoid restarts during short reconnections.
                        control('stop-worker')
                        failures = 0
                        raise RuntimeError('restarting after four failed health checks')
            except Exception as exc:
                state, reason = 'retrying', str(exc)
                delay = backoff
                backoff = min(60, backoff * 2)
            publish(state, reason)
            current = (state, reason)
            if current != previous:
                LOG.info('state=%s reason=%s next_check=%ds', state, reason, delay)
                previous = current
            STOP.wait(delay)
    finally:
        publish('stopped')
        LOG.info('supervisor stopped')


if __name__ == '__main__':
    cleanup_worker() if '--cleanup-worker' in sys.argv else main()
