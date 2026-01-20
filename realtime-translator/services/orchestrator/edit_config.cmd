@echo off
setlocal

cd /d "%~dp0"

set "SCRIPT_PATH=scripts\config_editor.py"

rem Prefer project venv if present
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
  python -m pip install --upgrade pip >nul 2>&1
  python -m pip install sounddevice >nul 2>&1
  python "%SCRIPT_PATH%"
) else (
  py -3 -m pip install --upgrade pip >nul 2>&1
  py -3 -m pip install sounddevice >nul 2>&1
  py -3 "%SCRIPT_PATH%"
)

pause
endlocal
