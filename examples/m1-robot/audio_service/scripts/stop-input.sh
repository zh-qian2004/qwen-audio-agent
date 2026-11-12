#!/usr/bin/env bash
set -eo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PID_FILE="$ROOT/data/audio_input_service.pid"
[[ -f "$PID_FILE" ]] || exit 0
PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  exit 0
fi
if ! tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "$ROOT/src/input_main.py"; then
  echo "PID $PID does not belong to audio_input_service" >&2
  rm -f "$PID_FILE"
  exit 1
fi
kill "$PID" 2>/dev/null || true
for _ in $(seq 1 100); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "audio_input_service stopped (PID $PID)"
    exit 0
  fi
  sleep 0.1
done
kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
