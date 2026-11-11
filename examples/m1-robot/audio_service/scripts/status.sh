#!/usr/bin/env bash
# 查看 audio_service 运行状态。
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PID_FILE="$ROOT/data/audio_service.pid"
PORT=8091
PY="$ROOT/.venv/bin/python"
# 厂商镜像里的 curl 依赖 /middleware/pub/lib 下的 libcurl
export LD_LIBRARY_PATH="/middleware/pub/lib:/app/joyin/lib:${LD_LIBRARY_PATH:-}"

echo "== 进程 =="
if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "running PID=$PID"
    tr '\0' ' ' < "/proc/$PID/cmdline"; echo
    echo "启动时长: $(ps -o etime= -p "$PID" | tr -d ' ')"
  else
    echo "PID 文件存在但进程不在：$PID"
  fi
else
  echo "not running（无 PID 文件）"
fi

echo
echo "== 监听地址 =="
if command -v ss >/dev/null 2>&1; then
  ss -ltnp 2>/dev/null | grep ":$PORT" || echo "未监听 $PORT"
else
  echo "ss 不可用"
fi

echo
echo "== /api/health =="
"$PY" - "$PORT" <<'PY' || echo "健康检查失败（服务未启动？）"
import json, sys, urllib.request
port = sys.argv[1]
try:
    data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3).read())
except Exception as exc:
    sys.exit(f"health 请求失败: {exc}")
print(json.dumps(data, ensure_ascii=False, indent=2))
PY

echo
echo "== /api/tts/status（摘要） =="
"$PY" - "$PORT" <<'PY' || echo "状态查询失败"
import json, sys, urllib.request
port = sys.argv[1]
try:
    d = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tts/status", timeout=3).read())
except Exception as exc:
    sys.exit(f"status 请求失败: {exc}")
print(json.dumps({
    "model": d.get("model"),
    "voice": d.get("voice"),
    "queue": d.get("queue"),
    "current": (d.get("current_task") or {}).get("phase"),
    "provider": {k: d["provider"].get(k) for k in ("available", "unavailable_reason", "last_error")},
    "ros": {k: d["ros"].get(k) for k in ("available", "subscribers", "published_packets")},
    "media_audio": d.get("media_audio"),
}, ensure_ascii=False, indent=2))
PY

echo
echo "== media_audio =="
ps -o pid,etime,cmd -C media_audio 2>/dev/null || echo "未发现 media_audio 进程"

echo
echo "== 最近日志 =="
tail -n 12 "$ROOT/logs/audio_service.log" 2>/dev/null || echo "暂无日志"
