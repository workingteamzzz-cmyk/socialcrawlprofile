#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PY=python3
command -v $PY >/dev/null 2>&1 || PY=python
if [ ! -d .venv ]; then
  echo "Menyiapkan lingkungan Python (sekali saja)..."
  $PY -m venv .venv
fi
source .venv/bin/activate
if [ ! -f .setup_done ] || [ requirements.txt -nt .setup_done ]; then
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
  python -m playwright install chromium
  touch .setup_done
fi
pip install -q --upgrade yt-dlp >/dev/null 2>&1 || true
python run.py
