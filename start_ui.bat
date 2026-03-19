@echo off
setlocal
cd /d "%~dp0"

if "%GROQ_API_KEY%"=="" (
  echo [WARN] GROQ_API_KEY не задан.
  echo Установите перед запуском, например:
  echo setx GROQ_API_KEY "your_key_here"
)

python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Не удалось установить зависимости.
  pause
  exit /b 1
)

start "" http://127.0.0.1:8000
python web_monitor.py
