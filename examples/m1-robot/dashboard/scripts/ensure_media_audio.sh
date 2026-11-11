#!/usr/bin/env bash
set -euo pipefail

if pgrep -x media_audio >/dev/null 2>&1; then
  exit 0
fi

mkdir -p /userdata/joyin/ros_home /userdata/log/media_audio/ros

# Keep recovery launches aligned with audio_service's active isolation config.
MEDIA_DOMAIN_ID=0
if [[ -r /userdata/audio_service/config/service.json && -x /userdata/audio_service/.venv/bin/python ]]; then
  MEDIA_DOMAIN_ID=$(/userdata/audio_service/.venv/bin/python -c '
import json
with open("/userdata/audio_service/config/service.json") as handle:
    config = json.load(handle)
bridge = config.get("media_audio_bridge", {})
print(int(bridge.get("domain_id", 43)) if bridge.get("enabled") else 0)
')
fi

cd /app/joyin/bin
env \
  ROS_VERSION=2 \
  ROS_PYTHON_VERSION=3 \
  ROS_DISTRO=humble \
  ROS_DOMAIN_ID="$MEDIA_DOMAIN_ID" \
  ROS_LOCALHOST_ONLY=1 \
  ROS_HOME=/userdata/joyin/ros_home \
  ROS_LOG_DIR=/userdata/log/media_audio/ros \
  FASTRTPS_DEFAULT_PROFILES_FILE=/app/joyin/config/fastdds_shm.xml \
  LD_LIBRARY_PATH=/opt/ros/humble/lib/aarch64-linux-gnu:/opt/ros/humble/lib:/app/joyin/lib:/app/joyin/lib/thirdparty:/middleware/lib:/middleware/pub/lib:/usr/hobot/lib:/usr/hobot/lib/sensor:/system/lib:/system/usr/lib:/lib \
  taskset -c 4 ./media_audio >/dev/null 2>&1 &

for _ in $(seq 1 50); do
  pgrep -x media_audio >/dev/null 2>&1 && exit 0
  sleep 0.1
done

echo "media_audio failed to start" >&2
exit 1
