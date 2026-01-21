@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

set "CONTAINER=oebb-pgvector"
set "VOLUME=oebb_pgdata"
set "DB=terminology"
set "USER=postgres"
set "PASS=oebb"
set "PORT=5432"
set "IMAGE=pgvector/pgvector:pg16"

echo === Postgres (Docker) Bootstrap ===
echo Working dir: %cd%
echo.

REM --- Check Docker is running ---
docker info >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
  echo ERROR: Docker does not seem to be running.
  echo Start Docker Desktop and retry.
  pause
  exit /b 1
)

REM --- Ensure volume exists ---
docker volume inspect "%VOLUME%" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
  echo Creating volume: %VOLUME%
  docker volume create "%VOLUME%" >nul
)

REM --- If container exists, start it; else create it ---
docker ps -a --format "{{.Names}}" | findstr /i /x "%CONTAINER%" >nul
if %ERRORLEVEL%==0 (
  echo Container exists: %CONTAINER%
  echo Starting container...
  docker start "%CONTAINER%" >nul
) else (
  echo Pulling image: %IMAGE%
  docker pull "%IMAGE%"
  echo Creating container: %CONTAINER%
  docker run -d --name "%CONTAINER%" ^
    -e POSTGRES_PASSWORD=%PASS% ^
    -e POSTGRES_DB=%DB% ^
    -p %PORT%:5432 ^
    -v "%VOLUME%":/var/lib/postgresql/data ^
    "%IMAGE%" >nul
)

REM --- Wait until Postgres is ready ---
echo Waiting for Postgres to become ready...
set /a tries=0
:wait_loop
set /a tries+=1
docker exec "%CONTAINER%" pg_isready -U "%USER%" -d "%DB%" >nul 2>nul
if %ERRORLEVEL%==0 goto ready
if %tries% GEQ 60 (
  echo ERROR: Postgres did not become ready in time.
  pause
  exit /b 1
)
timeout /t 1 >nul
goto wait_loop

:ready
echo Postgres is ready.

REM --- Apply schema (idempotent) ---
if not exist "db\schema.sql" (
  echo ERROR: Missing db\schema.sql
  pause
  exit /b 1
)

echo Applying schema...
docker exec -i "%CONTAINER%" psql -U "%USER%" -d "%DB%" < "db\schema.sql"
if %ERRORLEVEL% NEQ 0 (
  echo ERROR: Schema apply failed.
  pause
  exit /b 1
)

echo.
echo Done. Connection:
echo   postgresql://%USER%:%PASS%@127.0.0.1:%PORT%/%DB%
echo.
echo Press any key to close this window...
pause >nul
exit /b 0
