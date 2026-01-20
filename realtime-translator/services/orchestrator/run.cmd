@echo off
setlocal
cd /d "%~dp0"

echo [Launcher] Working dir:
cd
echo.

if not exist ".\.venv\Scripts\activate.bat" (
  color 0C
  echo ERROR: venv not found. Run setup.cmd first.
  pause
  exit /b 1
)

call ".\.venv\Scripts\activate.bat"

echo [Launcher] Python:
where python
python --version
echo.

REM ------------------------------------------------------------
REM Detect rag.backend from config.json (or OEBB_CONFIG) and
REM verify Docker availability for postgres backends
REM ------------------------------------------------------------
for /f "usebackq delims=" %%B in (`python -c "import os,json,pathlib; p=os.getenv('OEBB_CONFIG'); p=pathlib.Path(p) if p else pathlib.Path('config.json'); cfg=json.load(open(p,'r',encoding='utf-8')); print((cfg.get('rag',{}) or {}).get('backend','').strip().lower())"`) do set "RAG_BACKEND=%%B"

if "%RAG_BACKEND%"=="" set "RAG_BACKEND=glossary"

echo [Launcher] RAG backend: %RAG_BACKEND%

if "%RAG_BACKEND%"=="postgres"  goto :check_docker
if "%RAG_BACKEND%"=="postgres_vector" goto :check_docker
goto :start_app

:check_docker
echo [Launcher] Checking Docker...
docker info >nul 2>nul
if errorlevel 1 (
  color 0C
  echo.
  echo ERROR: Docker is not running or not available in PATH.
  echo Please start Docker Desktop and try again.
  echo.
  pause
  exit /b 1
)

REM Optional: check that the expected container exists (customize name if needed)
REM docker ps --format "{{.Names}}" | findstr /i "oebb-pgvector" >nul
REM if errorlevel 1 (
REM   echo WARNING: Container oebb-pgvector not running. If you use Postgres via Docker, start it first.
REM )

goto :start_app

:start_app
echo.
echo [Launcher] Starting app...
python realtime_translate.py
echo.

echo Exit code: %ERRORLEVEL%
pause
endlocal
