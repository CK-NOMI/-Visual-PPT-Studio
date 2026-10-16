#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "找不到 Python 3。请先安装 Python 3.11 或更高版本。"
  read -r -p "按回车键关闭窗口…"
  exit 1
fi

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "需要 Python 3.11 或更高版本。"
  read -r -p "按回车键关闭窗口…"
  exit 1
fi

# Match run.py's local .env behavior without sourcing or printing secrets.
if [[ -z "${PPT_STUDIO_PORT:-}" ]]; then
  PPT_STUDIO_PORT="$("$PY" - <<'PY'
from pathlib import Path
port = "8765"
for line in Path(".env").read_text(encoding="utf-8").splitlines() if Path(".env").exists() else []:
    if line.strip().startswith("PPT_STUDIO_PORT="):
        port = line.split("=", 1)[1].strip().strip("\"'")
print(port)
PY
)"
fi
export PPT_STUDIO_PORT
URL="http://127.0.0.1:${PPT_STUDIO_PORT}"

open_page() {
  if command -v open >/dev/null 2>&1; then
    open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
  else
    echo "请在浏览器打开：$URL"
  fi
}

if "$PY" - "$URL" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=1) as response:
        raise SystemExit(0 if response.status < 500 else 1)
except Exception:
    raise SystemExit(1)
PY
then
  echo "Visual PPT Studio 已经在运行，正在打开网页：$URL"
  open_page
  exit 0
fi

echo "正在启动 Visual PPT Studio…"
"$PY" -B run.py &
SERVER_PID=$!
stop_server() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap stop_server INT TERM

READY=0
for _ in $(seq 1 60); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID" || true
    echo "服务启动失败。请检查上面的错误信息。"
    read -r -p "按回车键关闭窗口…"
    exit 1
  fi
  if "$PY" - "$URL" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=1) as response:
        raise SystemExit(0 if response.status < 500 else 1)
except Exception:
    raise SystemExit(1)
PY
  then
    READY=1
    break
  fi
  sleep 1
done

if [[ "$READY" -ne 1 ]]; then
  echo "等待服务启动超时。请检查上面的错误信息。"
  stop_server
  wait "$SERVER_PID" || true
  read -r -p "按回车键关闭窗口…"
  exit 1
fi

echo "网页已启动：$URL"
open_page
echo "服务正在运行。关闭此终端窗口或按 Ctrl+C 可停止服务。"
wait "$SERVER_PID"
