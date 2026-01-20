@echo off
setlocal

cd /d "%~dp0"

set SCRIPT_PATH=scripts\voice_manager.py

rem Prefer project venv if present
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
  python "%SCRIPT_PATH%"
) else (
  py -3 "%SCRIPT_PATH%"
)

pause
endlocal
