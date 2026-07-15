@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ============================
REM Item Generator
REM ============================

cd /d "%~dp0"

if not exist "config.json" (
  echo [!] Missing config.json. Restore it beside app.py before starting.
  exit /b 1
)

if not exist "data\PF2e_Treasure_Generator_Backend.db" (
  echo [!] Missing data\PF2e_Treasure_Generator_Backend.db.
  echo [!] Restore the catalog database before starting.
  exit /b 1
)

set "VENV_PYTHON=.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo [*] Creating or repairing virtual environment...
  py -3.12 -m venv --clear .venv
  if errorlevel 1 (
    echo [!] Python could not create the virtual environment.
    echo [!] Install Python 3.12 with the Windows py launcher, then try again.
    exit /b 1
  )
)

echo [*] Activating venv...
call ".venv\Scripts\activate"
if errorlevel 1 (
  echo [!] The virtual environment could not be activated.
  exit /b 1
)

echo [*] Python info:
python -c "import sys,platform;print(sys.version);print(platform.platform())"
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 'Python 3.12 is required by .python-version')"
if errorlevel 1 exit /b 1

echo [*] Validating SQLite catalog contents...
python -c "import json; from services.catalog_validation import validate_catalog; c=json.load(open('config.json', encoding='utf-8')); r=validate_catalog(c['sqlite_db_path'], c['sqlite_view']); print('Catalog validated: %%d rows across %%d sources' %% (r['rows'], r['sources']))"
if errorlevel 1 (
  echo [!] Catalog validation failed. Restore a verified catalog before starting.
  exit /b 1
)

echo [*] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 exit /b 1

echo [*] Installing requirements (prefer wheels)...
python -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 (
  echo [!] Dependency installation failed. Check the network output above.
  exit /b 1
)

python -c "import flask,pandas,numpy"
if errorlevel 1 (
  echo [!] Required Python packages are still unavailable.
  exit /b 1
)

if "%LOOTGEN_DB_PATH%"=="" (
  set "LOOTGEN_DB_PATH=%~dp0data\PF2e_Treasure_Generator_Backend.db"
)

if "%LOOTGEN_STATE_DB_PATH%"=="" (
  set "LOOTGEN_STATE_DB_PATH=%~dp0data\player_views.db"
)

echo [*] Using DB: %LOOTGEN_DB_PATH%
echo [*] Player View state DB: %LOOTGEN_STATE_DB_PATH%
set FLASK_DEBUG=1

REM Open the browser after a short delay, without blocking this window.
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:5000"

echo [*] Starting app on http://localhost:5000
python app.py
