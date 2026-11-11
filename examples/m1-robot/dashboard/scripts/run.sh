#!/usr/bin/env bash
set -eo pipefail

ROOT=/userdata/developer/dashboard
PID_FILE="$ROOT/data/dashboard.pid"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    if tr '\0' ' ' < "/proc/$OLD_PID/cmdline" | grep -q "$ROOT/src/main.py"; then
      echo "Dashboard already running (PID $OLD_PID)"
      exit 0
    fi
  fi
  rm -f "$PID_FILE"
fi

export ROS_HOME="$ROOT/data/ros_home"
export ROS_LOG_DIR="$ROOT/logs"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export ROS_LOCALHOST_ONLY=1
export FASTRTPS_DEFAULT_PROFILES_FILE=/app/joyin/config/fastdds_shm.xml
export PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1

. /middleware/humble/setup.sh
export AMENT_PREFIX_PATH=/app/joyin:$AMENT_PREFIX_PATH
export PYTHONPATH=/app/joyin/local/lib/python3.10/dist-packages:$PYTHONPATH
export LD_LIBRARY_PATH=/middleware/pub/lib:/app/joyin/lib:/app/joyin/lib/thirdparty:$LD_LIBRARY_PATH
. "$ROOT/.venv/bin/activate"
set -u

mkdir -p "$ROOT/logs" "$ROOT/data/ros_home"
"$ROOT/scripts/ensure_media_audio.sh"
nohup "$ROOT/.venv/bin/python" "$ROOT/src/main.py" </dev/null >"$ROOT/logs/console.log" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

for _ in $(seq 1 150); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "Dashboard exited during startup; see $ROOT/logs/dashboard.log" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -fsS --max-time 1 http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo "Dashboard started (PID $PID), http://127.0.0.1:8080"
    exit 0
  fi
  sleep 0.1
done

echo "Dashboard health check failed; stopping PID $PID" >&2
kill "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
exit 1
