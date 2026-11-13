#!/usr/bin/env bash
set -euo pipefail
base=/userdata/developer/audio-agent
exec "$base/runtime/node/bin/node" \
  "$base/runtime/qwen-code-0.27.2/node_modules/@qwen-code/qwen-code/cli-entry.js" \
  --bare "$@"
