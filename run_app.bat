@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ============================
REM Item Generator
REM ============================

cd /d "%~dp0"

if not exist .venv (
  echo [*] Creating virtual environment...
  py -3 -m venv .venv
)

echo [*] Activating venv...
call ".venv\Scripts\activate"

echo [*] Python info:
python -c "import sys,platform;print(sys.version);print(platform.platform())"

echo [*] Upgrading pip...
python -m pip install --upgrade pip

echo [*] Installing requirements (prefer wheels)...
python -m pip install --prefer-binary -r requirements.txt

if "%LOOTGEN_DB_PATH%"=="" (
  set "LOOTGEN_DB_PATH=C:\Users\kkroe\Desktop\PF2e_Item_Generator\data\PF2e_Treasure_Generator_Backend.db"
)

echo [*] Using DB: %LOOTGEN_DB_PATH%
set FLASK_ENV=development

REM Open the browser after a short delay, without blocking this window.
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:5000"

echo [*] Starting app on http://localhost:5000
python app.py