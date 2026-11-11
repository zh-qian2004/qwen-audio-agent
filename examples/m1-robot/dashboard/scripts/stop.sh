#!/usr/bin/env bash
set -euo pipefail

ROOT=/userdata/developer/dashboard
PID_FILE="$ROOT/data/dashboard.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Dashboard is not running (no PID file)"
  exit 0
fi

PID=$(cat "$PID_FILE")
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Dashboard is not running (stale PID file removed)"
  exit 0
fi

if ! tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "$ROOT/src/main.py"; then
  echo "PID $PID does not belong to the Dashboard; PID file left unchanged" >&2
  exit 1
fi

kill "$PID"
for _ in $(seq 1 100); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Dashboard stopped"
    exit 0
  fi
  sleep 0.1
done

kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "Dashboard stopped after forced termination"
