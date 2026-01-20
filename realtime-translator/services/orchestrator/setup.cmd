@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo === Realtime Translator Setup [Windows] ===
echo Working dir: %cd%
echo.

REM ------------------------------------------------------------
REM Centralized error handling: jump to :done on any failure
REM ------------------------------------------------------------
set "EXITCODE=0"

REM --- Detect Python (prefer py launcher) ---
set "PYEXE="

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PYEXE=py"
) else (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 (
    set "PYEXE=python"
  )
)

if "%PYEXE%"=="" (
  echo ERROR: Python not found in PATH.
  echo Install Python 3.11 recommended and try again.
  echo If winget is available you can run:
  echo   winget install -e --id Python.Python.3.11
  set "EXITCODE=1"
  goto :done
)

echo Using: %PYEXE%
%PYEXE% --version
echo.

REM --- Create venv if missing ---
if not exist ".venv\Scripts\activate.bat" (
  echo Creating virtual environment...
  %PYEXE% -m venv .venv
  if %ERRORLEVEL% NEQ 0 (
    echo ERROR: venv creation failed.
    set "EXITCODE=1"
    goto :done
  )
)

call ".venv\Scripts\activate.bat"
if %ERRORLEVEL% NEQ 0 (
  echo ERROR: Could not activate venv.
  set "EXITCODE=1"
  goto :done
)

echo.
echo Upgrading pip...
python -m pip install --upgrade pip
if %ERRORLEVEL% NEQ 0 (
  echo ERROR: pip upgrade failed.
  set "EXITCODE=1"
  goto :done
)

echo.
echo Installing dependencies...
if exist "requirements-lock.txt" (
  python -m pip install -r requirements-lock.txt
) else (
  python -m pip install -r requirements.txt
)

if %ERRORLEVEL% NEQ 0 (
  echo ERROR: pip install failed.
  set "EXITCODE=1"
  goto :done
)

echo.
echo Checking basic imports...
python -c "import numpy, sounddevice, webrtcvad, psycopg; print('imports ok')"
if %ERRORLEVEL% NEQ 0 (
  echo ERROR: Import check failed [core imports].
  set "EXITCODE=1"
  goto :done
)

python -c "import openpyxl, pgvector, sentence_transformers; print('terminology imports ok')"
if %ERRORLEVEL% NEQ 0 (
  echo ERROR: Import check failed [terminology imports].
  set "EXITCODE=1"
  goto :done
)

echo.
echo Checking config.json...
if not exist "config.json" (
  echo ERROR: config.json not found next to this script.
  set "EXITCODE=1"
  goto :done
)

echo.
echo Setup finished.
echo Next:
echo - Ensure Piper voice model exists at path configured in config.json
echo - Ensure Argos models are installed, run scripts if needed
echo - Optional: Ensure Docker and Postgres database are running
echo - Optional: Run setup_terminology.cmd for initial setup of Postgres (and vector) terminology.
echo - Start with run.cmd
echo.

:done
echo.
if "%EXITCODE%"=="0" (
  echo Exit code: 0 - success
) else (
  echo Exit code: %EXITCODE% - failed
)

echo Press any key to close this window...
pause >nul
exit /b %EXITCODE%
