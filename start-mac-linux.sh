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
  touch .setup_done
fi
pip install -q -r requirements.txt >/dev/null 2>&1 || true   # picks up new requirements after an update
# Quick when already installed; retried on every start if an earlier download failed.
python -m playwright install chromium >/dev/null 2>&1 || echo "Note: Chromium could not be downloaded - the app will use Google Chrome or Edge if installed."
python run.py
