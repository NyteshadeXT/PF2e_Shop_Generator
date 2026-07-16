@echo off
setlocal

REM ============================
REM Item Generator
REM ============================

cd /d "%~dp0"

if not exist ".python-version" (
  echo [!] Missing .python-version. Restore it beside app.py before starting.
  goto :startup_failed
)
set /p "REQUIRED_PYTHON="<.python-version
if "%REQUIRED_PYTHON%"=="" (
  echo [!] .python-version is empty.
  goto :startup_failed
)

if not exist "config.json" (
  echo [!] Missing config.json. Restore it beside app.py before starting.
  goto :startup_failed
)

if not exist "data\PF2e_Treasure_Generator_Backend.db" (
  echo [!] Missing data\PF2e_Treasure_Generator_Backend.db.
  echo [!] Restore the catalog database before starting.
  goto :startup_failed
)

set "VENV_PYTHON=.venv\Scripts\python.exe"
set "REBUILD_VENV=0"
if not exist "%VENV_PYTHON%" set "REBUILD_VENV=1"
if exist "%VENV_PYTHON%" (
  "%VENV_PYTHON%" -c "import sys; expected=tuple(map(int, '%REQUIRED_PYTHON%'.split('.'))); raise SystemExit(0 if sys.version_info[:2] == expected else 1)" >nul 2>&1
  if errorlevel 1 (
    echo [*] Existing virtual environment uses the wrong Python version.
    set "REBUILD_VENV=1"
  )
)

if "%REBUILD_VENV%"=="1" (
  py -%REQUIRED_PYTHON% -c "import sys; print(sys.version)" >nul 2>&1
  if errorlevel 1 (
    echo [!] Python %REQUIRED_PYTHON% is not installed or is not registered with the Windows py launcher.
    echo [!] Install 64-bit Python %REQUIRED_PYTHON% from https://www.python.org/downloads/
    echo [!] During installation, enable the Python launcher, then run this file again.
    goto :startup_failed
  )
  echo [*] Creating or repairing virtual environment...
  py -%REQUIRED_PYTHON% -m venv --clear .venv
  if errorlevel 1 (
    echo [!] Python could not create the virtual environment.
    echo [!] Install Python %REQUIRED_PYTHON% with the Windows py launcher, then try again.
    goto :startup_failed
  )
)

echo [*] Activating venv...
call ".venv\Scripts\activate"
if errorlevel 1 (
  echo [!] The virtual environment could not be activated.
  goto :startup_failed
)

echo [*] Python info:
python -c "import sys,platform;print(sys.version);print(platform.platform())"
python -c "import sys; expected=tuple(map(int, '%REQUIRED_PYTHON%'.split('.'))); raise SystemExit(0 if sys.version_info[:2] == expected else 'Python %REQUIRED_PYTHON% is required by .python-version')"
if errorlevel 1 goto :startup_failed

echo [*] Validating SQLite catalog contents...
python -c "import json; from services.catalog_validation import validate_catalog; c=json.load(open('config.json', encoding='utf-8')); r=validate_catalog(c['sqlite_db_path'], c['sqlite_view']); print('Catalog validated: %%d rows across %%d sources' %% (r['rows'], r['sources']))"
if errorlevel 1 (
  echo [!] Catalog validation failed. Restore a verified catalog before starting.
  goto :startup_failed
)

echo [*] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 goto :startup_failed

echo [*] Installing requirements (prefer wheels)...
python -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 (
  echo [!] Dependency installation failed. Check the network output above.
  goto :startup_failed
)

python -c "import flask,pandas,numpy"
if errorlevel 1 (
  echo [!] Required Python packages are still unavailable.
  goto :startup_failed
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
if errorlevel 1 goto :startup_failed
goto :eof

:startup_failed
echo.
echo [!] The PF2e Shop Generator could not start. Review the message above.
echo [!] This window will remain open so the error can be read.
pause
exit /b 1
