#!/usr/bin/env bash
# Boot entry for the M1 developer services.
# /etc/init.d/S99auto_startup launches this file automatically when it is
# installed as /userdata/startup.sh and marked executable.

set -uo pipefail

LOG_DIR=/userdata/log
LOG_FILE="$LOG_DIR/developer-services-startup.log"
LOCK_DIR=/userdata/.developer-services-startup.lock.d

mkdir -p "$LOG_DIR"

# Keep the boot log bounded without relying on logrotate.
if [[ -f "$LOG_FILE" ]] && [[ $(wc -c < "$LOG_FILE") -gt 1048576 ]]; then
  mv -f "$LOG_FILE" "$LOG_FILE.1"
fi

exec >>"$LOG_FILE" 2>&1

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date '+%F %T')] another developer-service startup is already running"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

echo "[$(date '+%F %T')] starting M1 developer services"

# S99auto_startup may run while the vendor stack is still settling.  Wait for
# its local HTTP control plane, but do not prevent our services from starting
# if the vendor endpoint remains unavailable.
vendor_ready=0
for _ in $(seq 1 90); do
  if LD_LIBRARY_PATH=/app/joyin/lib/thirdparty:/middleware/pub/lib:${LD_LIBRARY_PATH:-} \
      curl -sS --max-time 1 http://127.0.0.1:12321/ >/dev/null 2>&1; then
    vendor_ready=1
    break
  fi
  sleep 1
done
echo "[$(date '+%F %T')] vendor control ready=$vendor_ready"

health_ok() {
  local url=$1
  LD_LIBRARY_PATH=/app/joyin/lib/thirdparty:/middleware/pub/lib:${LD_LIBRARY_PATH:-} \
    curl -fsS --max-time 3 "$url" >/dev/null 2>&1
}

start_service() {
  local name=$1
  local run_script=$2
  local health_url=$3

  if [[ ! -x "$run_script" ]]; then
    echo "[$(date '+%F %T')] $name: missing executable $run_script"
    return 1
  fi

  if health_ok "$health_url"; then
    echo "[$(date '+%F %T')] $name: already healthy"
    return 0
  fi

  local attempt
  for attempt in 1 2 3; do
    echo "[$(date '+%F %T')] $name: start attempt $attempt"
    "$run_script" || true

    for _ in $(seq 1 20); do
      if health_ok "$health_url"; then
        echo "[$(date '+%F %T')] $name: healthy"
        return 0
      fi
      sleep 1
    done
  done

  echo "[$(date '+%F %T')] $name: failed health check $health_url"
  return 1
}

# Start gateways before Dashboard so its first dependency probe sees them.
start_service audio_service \
  /userdata/audio_service/scripts/run.sh \
  http://127.0.0.1:8091/api/health || true

start_service vision_service \
  /userdata/vision_service/scripts/run.sh \
  http://127.0.0.1:8092/api/health || true

start_service dashboard \
  /userdata/developer/dashboard/scripts/run.sh \
  http://127.0.0.1:8080/api/health || true

echo "[$(date '+%F %T')] M1 developer-service startup complete"
