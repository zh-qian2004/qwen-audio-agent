#!/usr/bin/env bash
set -eo pipefail

ROOT=${VISION_SERVICE_ROOT:-/userdata/vision_service}
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" && -x /userdata/developer/dashboard/.venv/bin/python ]]; then
  PYTHON=/userdata/developer/dashboard/.venv/bin/python
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "No usable Python environment" >&2
  exit 1
fi

. /middleware/humble/setup.sh
export AMENT_PREFIX_PATH=/app/joyin:$AMENT_PREFIX_PATH
export PYTHONPATH=/app/joyin/local/lib/python3.10/dist-packages:$PYTHONPATH
export LD_LIBRARY_PATH=/middleware/pub/lib:/app/joyin/lib:/app/joyin/lib/thirdparty:$LD_LIBRARY_PATH

cd "$ROOT"
"$PYTHON" -m unittest discover -s tests -p 'test_*.py' -v
"$PYTHON" -m py_compile \
  src/main.py src/service.py src/ros_runtime.py src/result_bus.py src/metrics.py \
  src/processors/base.py src/processors/object_detection.py
