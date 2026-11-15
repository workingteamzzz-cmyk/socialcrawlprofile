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
echo "1/3 Checking the app's packages (after an update this installs the new ones - a few minutes)..."
pip install -q -r requirements.txt >/dev/null 2>&1 || true   # picks up new requirements after an update
# Quick when already installed; retried on every start if an earlier download failed.
echo "2/3 Checking the browser (after an update it may download Chromium, ~150 MB - please wait)..."
python -m playwright install chromium >/dev/null 2>&1 || echo "Note: Chromium could not be downloaded - the app will use Google Chrome or Edge if installed."
echo "3/3 Starting - the app opens in your browser at http://127.0.0.1:8770"
python run.py
