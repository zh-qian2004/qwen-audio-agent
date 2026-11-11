#!/usr/bin/env bash
# 停止 audio_service。只停止本项目（cmdline 校验通过）的进程。
set -eo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PID_FILE="$ROOT/data/audio_service.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "audio_service 未运行（无 PID 文件）"
  exit 0
fi

PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -z "$PID" ]]; then
  rm -f "$PID_FILE"
  echo "PID 文件为空，已清理"
  exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "进程 $PID 不存在，已清理 PID 文件"
  exit 0
fi

if ! tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "$ROOT/src/main.py"; then
  echo "PID $PID 不属于 audio_service，拒绝停止" >&2
  rm -f "$PID_FILE"
  exit 1
fi

kill "$PID" 2>/dev/null || true
for _ in $(seq 1 100); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "audio_service stopped (PID $PID)"
    exit 0
  fi
  sleep 0.1
done

echo "audio_service 未在 10 秒内退出，发送 SIGKILL" >&2
kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
exit 0
