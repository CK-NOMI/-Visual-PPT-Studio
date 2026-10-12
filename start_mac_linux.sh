#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if command -v python3.11 >/dev/null 2>&1; then PY=python3.11
elif command -v python3.12 >/dev/null 2>&1; then PY=python3.12
else PY=python3; fi
if ! "$PY" -c 'import sys;assert sys.version_info >= (3,11)' 2>/dev/null; then
 echo '需要 Python >= 3.11。macOS 建议 brew install python@3.11 并重试。' >&2;exit 2
fi
if [[ ! -d .venv ]]; then "$PY" -m venv .venv; fi
source .venv/bin/activate
if ! python -c 'import sys;assert sys.version_info >= (3,11)' 2>/dev/null; then
 echo '现有 .venv 的 Python 版本低于 3.11：请先备份并删除 .venv，然后重试。' >&2;exit 3
fi
python -m pip install -r requirements.txt
# Default renderer is python-pptx; Node/npm is optional and no longer blocks startup.
if [[ "${PPT_RENDERER:-python}" == "node" ]]; then
  if ! command -v node >/dev/null 2>&1; then echo 'PPT_RENDERER=node 要求安装Node.js' >&2;exit 5;fi
  if ! (cd renderer && node -e "require('pptxgenjs')" >/dev/null 2>&1); then (cd renderer && npm install --no-audit --no-fund); fi
fi
python run.py
