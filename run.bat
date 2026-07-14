@echo off
setlocal

cd /d "%~dp0"

echo ========================================
echo  Delta to Mirror Pip Copier
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv. Make sure Python is installed and on PATH.
    pause
    exit /b 1
  )
  echo Virtual environment created.
) else (
  echo [1/3] Virtual environment found.
)

echo.
echo [2/3] Installing requirements...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install requirements.
  pause
  exit /b 1
)

echo.
echo [3/3] Starting Flask app on http://0.0.0.0:5050
echo Local:  http://127.0.0.1:5050
echo Remote: http://YOUR-SERVER-IP:5050
echo Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" app.py

pause
endlocal
