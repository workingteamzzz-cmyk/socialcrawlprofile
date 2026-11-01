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
python -m playwright install chromium >nul 2>nul || echo Note: Chromium could not be downloaded - the app will use Google Chrome or Edge if installed.
pip install -q --upgrade yt-dlp >nul 2>nul
python run.py
pause
