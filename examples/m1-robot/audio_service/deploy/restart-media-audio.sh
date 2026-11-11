#!/bin/bash
set -euo pipefail

# Reuse the vendor launch environment without running run.sh prepare_env/start.
reference_pid=$(pgrep -x media_video | head -n 1)
if [[ -z "${reference_pid}" || ! -r "/proc/${reference_pid}/environ" ]]; then
    echo "Cannot locate the running vendor media environment" >&2
    exit 1
fi
while IFS= read -r -d '' entry; do
    case "${entry}" in
        ROS_*=*|RMW_*=*|FASTRTPS_*=*|AMENT_PREFIX_PATH=*|PYTHONPATH=*|LD_LIBRARY_PATH=*|PATH=*)
            export "${entry}"
            ;;
    esac
done < "/proc/${reference_pid}/environ"

media_domain="${1:-0}"
if [[ "${media_domain}" != 0 && "${media_domain}" != 43 ]]; then
    echo "Expected domain 0 or 43" >&2
    exit 1
fi
watchdog_pids=$(pgrep -f '^/bin/bash /app/joyin/watchdog.sh 10$' || true)
if [[ -n "${watchdog_pids}" ]]; then
    kill ${watchdog_pids}
fi
pkill -x media_audio || true
for attempt in 1 2 3 4 5; do
    if ! pgrep -x media_audio >/dev/null; then break; fi
    sleep 1
done
if pgrep -x media_audio >/dev/null; then pkill -KILL -x media_audio; fi

cd /app/joyin/bin
nohup env ROS_DOMAIN_ID="${media_domain}" taskset -c 4 ./media_audio \
    > /tmp/media_audio_recovery.log 2>&1 < /dev/null &
media_pid=$!
sleep 4
if ! kill -0 "${media_pid}" 2>/dev/null; then
    tail -40 /tmp/media_audio_recovery.log >&2
    exit 1
fi
nohup /bin/bash /app/joyin/watchdog.sh 10 \
    > /tmp/joyin_watchdog.log 2>&1 < /dev/null &
echo "media_audio pid=${media_pid} domain=${media_domain}; watchdog pid=$!"
