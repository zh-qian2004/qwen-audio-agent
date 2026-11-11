#!/usr/bin/env bash
set -e

ROOT=/userdata/vision_service
PID_FILE="$ROOT/data/vision_service.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "vision_service is not running"
  exit 0
fi

PID=$(cat "$PID_FILE")
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null && \
   tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "$ROOT/src/main.py"; then
  kill "$PID"
  for _ in $(seq 1 50); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -KILL "$PID"
  fi
fi
rm -f "$PID_FILE"
echo "vision_service stopped"
