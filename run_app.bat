@echo off
cd /d "C:\Users\kkroe\Desktop\PF2e_Item_Generator" || (echo [ERROR] Can't cd & pause & exit /b 1)
set "PORT=5000"

where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (echo [ERROR] Python not found & pause & exit /b 1)

if not exist "venv\Scripts\activate.bat" (
  echo [Setup] Creating venv...
  %PY% -m venv venv || (echo venv creation failed & pause & exit /b 1)
)
call "venv\Scripts\activate.bat" || (echo venv activation failed & pause & exit /b 1)

python -m pip install --upgrade pip >nul
pip install -r requirements.txt || (echo pip install failed & pause & exit /b 1)

echo [Info] starting server on http://127.0.0.1:%PORT% ...
start "PF2e Item Generator" cmd /k "call venv\Scripts\activate.bat && python app.py"
start "" "http://127.0.0.1:%PORT%"