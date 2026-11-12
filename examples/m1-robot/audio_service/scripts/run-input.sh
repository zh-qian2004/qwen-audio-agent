#!/usr/bin/env bash
# Start the isolated microphone WebSocket service used by the Agent.
set -eo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PID_FILE="$ROOT/data/audio_input_service.pid"
PY="$ROOT/.venv/bin/python"
PORT=8093

if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null \
      && tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "$ROOT/src/input_main.py"; then
    echo "audio_input_service already running (PID $PID)"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

export ROS_HOME="$ROOT/data/ros_home_input"
export ROS_LOG_DIR="$ROOT/logs"
export ROS_DOMAIN_ID=0
export ROS_LOCALHOST_ONLY=1
export FASTRTPS_DEFAULT_PROFILES_FILE=/app/joyin/config/fastdds_shm.xml
export PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
if [[ -f /middleware/humble/setup.sh ]]; then
  # shellcheck disable=SC1091
  . /middleware/humble/setup.sh
fi
export AMENT_PREFIX_PATH="/app/joyin:${AMENT_PREFIX_PATH:-}"
export PYTHONPATH="/app/joyin/local/lib/python3.10/dist-packages:${PYTHONPATH:-}"
export LD_LIBRARY_PATH="/middleware/pub/lib:/app/joyin/lib:/app/joyin/lib/thirdparty:${LD_LIBRARY_PATH:-}"

mkdir -p "$ROOT/logs" "$ROOT/data/ros_home_input"
nohup "$PY" "$ROOT/src/input_main.py" </dev/null >>"$ROOT/logs/input_console.log" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

for _ in $(seq 1 200); do
  if ! kill -0 "$PID" 2>/dev/null; then
    tail -n 30 "$ROOT/logs/input_console.log" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/api/health" 2>/dev/null \
      | grep -q '"service":"audio_input_service"'; then
    echo "audio_input_service started (PID $PID), http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 0.1
done

kill "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "audio_input_service health check failed" >&2
exit 1
