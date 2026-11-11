#!/usr/bin/env bash
# 启动 audio_service。可重复执行：已在运行则直接返回 0。
set -eo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PID_FILE="$ROOT/data/audio_service.pid"
CONFIG="$ROOT/config/service.json"
ENV_FILE="$ROOT/config/runtime.env"
PY="$ROOT/.venv/bin/python"
PORT=8091
LOG_MAX_BYTES=5242880

# ---------- 1. 防止重复启动 ----------
if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    if tr '\0' ' ' < "/proc/$OLD_PID/cmdline" | grep -q "$ROOT/src/main.py"; then
      echo "audio_service already running (PID $OLD_PID)"
      exit 0
    fi
  fi
  rm -f "$PID_FILE"
fi

# ---------- 2. 配置与依赖检查 ----------
if [[ ! -x "$PY" ]]; then
  echo "错误：虚拟环境缺失 $PY，请执行 python3 -m venv $ROOT/.venv 并安装 config/requirements.txt" >&2
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "错误：缺少配置文件 $CONFIG" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "错误：找不到 ffmpeg（MP3 解码依赖）" >&2
  exit 1
fi
# uvicorn 的 WebSocket 实现依赖 websockets，缺失时输入/输出 WebSocket 会直接 404
if ! "$PY" -c "import websockets" >/dev/null 2>&1; then
  echo "错误：虚拟环境缺少 websockets（WebSocket 输入流依赖），请安装 config/requirements.txt" >&2
  exit 1
fi

# ---------- 3. 运行密钥 ----------
if [[ ! -f "$ENV_FILE" ]]; then
  echo "错误：缺少运行环境文件 $ENV_FILE（需包含 DASHSCOPE_API_KEY=...）" >&2
  exit 1
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true
set +u
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
if [[ -z "${DASHSCOPE_API_KEY:-}" ]]; then
  echo "错误：$ENV_FILE 中未设置 DASHSCOPE_API_KEY" >&2
  exit 1
fi

# ---------- 4. 端口占用检查 ----------
if "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))==0 else 1)"; then
  echo "错误：127.0.0.1:$PORT 已被其他进程占用" >&2
  exit 1
fi

# ---------- 5. ROS / 厂商库环境（与 Dashboard 保持一致） ----------
export ROS_HOME="$ROOT/data/ros_home"
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

# 根文件系统没有 /usr/lib/ssl/cert.pem，wss:// 需要显式 CA 包
if [[ -z "${SSL_CERT_FILE:-}" ]]; then
  if [[ -f "$ROOT/.venv/lib/python3.10/site-packages/certifi/cacert.pem" ]]; then
    export SSL_CERT_FILE="$ROOT/.venv/lib/python3.10/site-packages/certifi/cacert.pem"
  elif [[ -f /etc/ssl/certs/ca-certificates.crt ]]; then
    export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  fi
  export REQUESTS_CA_BUNDLE="${SSL_CERT_FILE:-}"
  export WEBSOCKET_CLIENT_CA_BUNDLE="${SSL_CERT_FILE:-}"
fi

mkdir -p "$ROOT/logs" "$ROOT/data/ros_home"

# ---------- 6. 控制台日志轮转 ----------
CONSOLE_LOG="$ROOT/logs/console.log"
if [[ -f "$CONSOLE_LOG" ]]; then
  SIZE=$(stat -c %s "$CONSOLE_LOG" 2>/dev/null || echo 0)
  if [[ "$SIZE" -gt "$LOG_MAX_BYTES" ]]; then
    mv -f "$CONSOLE_LOG" "$CONSOLE_LOG.1"
  fi
fi

# ---------- 7. 启动 ----------
nohup "$PY" "$ROOT/src/main.py" </dev/null >>"$CONSOLE_LOG" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

for _ in $(seq 1 200); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "audio_service 启动过程中退出，最近日志：" >&2
    tail -n 20 "$CONSOLE_LOG" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "audio_service started (PID $PID), http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 0.1
done

echo "健康检查失败，停止 PID $PID" >&2
kill "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
exit 1
