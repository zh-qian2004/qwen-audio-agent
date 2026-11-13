#!/usr/bin/env bash
set -euo pipefail

base=/userdata/developer/audio-agent
release_name=$(<"$base/run/active-release")
[[ $release_name =~ ^[a-zA-Z0-9._-]+$ ]] || { echo 'invalid active release' >&2; exit 1; }
release="$base/releases/$release_name"
pidfile="$base/run/agent.pid"
logfile="$base/logs/agent.log"
node="$base/runtime/node/bin/node"
pausefile="$base/run/manual-stop"

# The supervisor has its own lifetime lock; worker controls are serialized here.
case ${1:-} in
  supervise-start)
    rm -f "$pausefile"
    nohup /usr/bin/python3 "$base/scripts/edge-supervisor.py" </dev/null >/dev/null 2>&1 &
    echo 'edge Agent supervisor requested'
    exit 0 ;;
  supervise-status)
    cat "$base/run/supervisor-state.json" 2>/dev/null || true
    exit 0 ;;
esac
exec 9>"$base/run/service.lock"
flock -w 160 9 || { echo 'Agent control is busy' >&2; exit 1; }

running() {
  [[ -f $pidfile ]] || return 1
  local pid
  pid=$(<"$pidfile")
  [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 1
  local command
  command=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null) || return 1
  [[ $command == *"node examples/m1-robot/start.mjs"* || $command == *"$base/scripts/edge-run.sh"* ]]
}

case ${1:-} in
  start|start-worker)
    if [[ $1 == start ]]; then rm -f "$pausefile"; fi
    if running; then echo "edge Agent already running: $(<"$pidfile")"; exit 0; fi
    "$node" "$release/edge-preflight.mjs"
    if [[ -f $logfile ]] && [[ $(wc -c < "$logfile") -gt 10485760 ]]; then
      mv "$logfile" "$logfile.1"
    fi
    nohup bash "$base/scripts/edge-run.sh" >>"$logfile" 2>&1 </dev/null 9>&- &
    echo "$!" >"$pidfile"
    for _ in $(seq 1 60); do
      if ! running; then
        echo 'edge Agent exited during startup'
        tail -n 15 "$logfile"
        exit 1
      fi
      if "$node" "$release/edge-health.mjs" >/dev/null 2>&1; then
        echo "edge Agent ready: $(<"$pidfile")"
        exit 0
      fi
      sleep 1
    done
    echo 'edge Agent did not become ready in 60 seconds'
    exit 1
    ;;
  status)
    echo "active release: $release_name"
    if running; then
      echo "edge Agent process: $(<"$pidfile")"
      "$node" "$release/edge-health.mjs"
    else
      echo 'edge Agent stopped'
    fi
    ;;
  stop|stop-worker)
    if [[ $1 == stop ]]; then touch "$pausefile"; fi
    if ! running; then echo 'edge Agent already stopped'; exit 0; fi
    pid=$(<"$pidfile")
    kill -TERM "$pid"
    for _ in $(seq 1 15); do
      if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pidfile"
        echo 'edge Agent stopped'
        exit 0
      fi
      sleep 1
    done
    echo "edge Agent PID $pid did not stop; inspect before retrying" >&2
    exit 1
    ;;
  *) echo "usage: $0 {start|status|stop|supervise-start|supervise-status}" >&2; exit 2 ;;
esac
