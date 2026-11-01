#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
PY=python3
command -v $PY >/dev/null 2>&1 || PY=python
if ! command -v $PY >/dev/null 2>&1; then
  echo "Python is not installed. Install Python 3.10+ from https://www.python.org/downloads/ and run this again."
  exit 1
fi
if [ ! -d .venv ]; then
  echo "Setting up the Python environment (first run only)..."
  $PY -m venv .venv
fi
source .venv/bin/activate
if [ ! -f .setup_done ] || [ requirements.txt -nt .setup_done ]; then
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
  python -m playwright install chromium || echo "Note: Chromium download failed - the app will use Google Chrome or Edge if installed."
  touch .setup_done
fi
pip install -q --upgrade yt-dlp >/dev/null 2>&1 || true
python run.py
