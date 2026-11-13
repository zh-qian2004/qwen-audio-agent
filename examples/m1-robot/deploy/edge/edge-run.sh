#!/usr/bin/env bash
set -euo pipefail

base=/userdata/developer/audio-agent
release_name=$(<"$base/run/active-release")
[[ $release_name =~ ^[a-zA-Z0-9._-]+$ ]] || { echo 'invalid active release' >&2; exit 1; }
release="$base/releases/$release_name"
export PATH="$base/runtime/node/bin:$base/runtime/qwen-code-0.27.2/node_modules/.bin:$PATH"
export TMPDIR="$base/run/tmp"
export XDG_CONFIG_HOME="$base/config"
export QWAUDIO_CONFIG_DIR="$base/config"
export QWAUDIO_DATA_DIR="$base/data"
export QWAUDIO_STATE_DIR="$base/run"
export QWAUDIO_CACHE_DIR="$base/data/cache"
export QWAUDIO_WORKSPACE="$base/data/workspace"
export QWEN_CODE_BIN="$base/scripts/qwen-bare.sh"

set -a
# shellcheck disable=SC1091
source <(tr -d '\r' < "$base/config/edge-credentials.env")
set +a
export QWEN_AUDIO_AGENT_BACKEND_MODEL="${QWEN_AUDIO_AGENT_BACKEND_MODEL:-qwen3.8-flash}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
export OPENAI_MODEL="$QWEN_AUDIO_AGENT_BACKEND_MODEL"
export OPENAI_API_KEY="${OPENAI_API_KEY:-$QWEN_AUDIO_REALTIME_API_KEY}"
export AGENT_PROTOCOL=qwen

cd "$release"
node edge-preflight.mjs
exec node examples/m1-robot/start.mjs \
  --config examples/m1-robot/m1-robot.config.edge.json --no-build
