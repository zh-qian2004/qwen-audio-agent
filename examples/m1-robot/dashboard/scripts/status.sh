#!/usr/bin/env bash
set -euo pipefail

ROOT=/userdata/developer/dashboard
PID_FILE="$ROOT/data/dashboard.pid"

if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null && tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "$ROOT/src/main.py"; then
    echo "Dashboard running (PID $PID)"
    LD_LIBRARY_PATH=/app/joyin/lib/thirdparty:/middleware/pub/lib:${LD_LIBRARY_PATH:-} curl -fsS --max-time 3 http://127.0.0.1:8080/api/health
    echo
    exit 0
  fi
fi

echo "Dashboard is not running"
exit 1
