#!/usr/bin/env bash
set -e

ROOT=/userdata/vision_service
PID_FILE="$ROOT/data/vision_service.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "vision_service stopped"
  exit 1
fi
PID=$(cat "$PID_FILE")
if ! kill -0 "$PID" 2>/dev/null; then
  echo "vision_service stale PID file ($PID)"
  exit 1
fi
echo "vision_service running (PID $PID)"
busybox wget -qO- --timeout=2 http://127.0.0.1:8092/api/health
echo
