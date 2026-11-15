@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title Post Link Collector
where python >nul 2>nul || (echo Python is not installed. Install it from https://www.python.org/downloads/ and tick "Add Python to PATH". & pause & exit /b 1)
if not exist .venv (echo Setting up Python environment, first run only... & python -m venv .venv || (pause & exit /b 1))
call .venv\Scripts\activate.bat
if not exist .setup_done (
  python -m pip install --upgrade pip
  pip install -r requirements.txt || (echo Package install failed - check your internet connection and run this file again. & pause & exit /b 1)
  echo done> .setup_done
)
rem Quick when everything is installed; picks up new requirements after an update.
echo 1/3 Checking the app's packages (after an update this installs the new ones - a few minutes)...
pip install -q -r requirements.txt >nul 2>nul
echo 2/3 Checking the browser (after an update it may download Chromium, about 150 MB - please wait)...
python -m playwright install chromium >nul 2>nul || echo Note: Chromium could not be downloaded - the app will use Google Chrome or Edge if installed.
echo 3/3 Starting - the app opens in your browser at http://127.0.0.1:8770
python run.py
pause
