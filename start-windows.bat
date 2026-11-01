@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python belum terpasang. Install dari https://www.python.org/downloads/ dan centang "Add Python to PATH". & pause & exit /b 1)
if not exist .venv (echo Menyiapkan lingkungan Python, sekali saja... & python -m venv .venv)
call .venv\Scripts\activate.bat
if not exist .setup_done (
  python -m pip install --upgrade pip
  pip install -r requirements.txt || (pause & exit /b 1)
  python -m playwright install chromium
  echo done> .setup_done
)
pip install -q --upgrade yt-dlp >nul 2>nul
python run.py
pause
