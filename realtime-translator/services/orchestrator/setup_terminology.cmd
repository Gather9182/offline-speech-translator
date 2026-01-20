@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

where docker >nul 2>&1
if errorlevel 1 (
  echo ERROR: Docker command not found.
  pause
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo ERROR: Docker not running. Start Docker Desktop.
  pause
  exit /b 1
)

set CONTAINER_NAME=oebb-pgvector

for /f "delims=" %%i in ('docker ps --filter "name=%CONTAINER_NAME%" --format "{{.Names}}"') do set RUNNING_NAME=%%i
if /i not "%RUNNING_NAME%"=="%CONTAINER_NAME%" (
  echo ERROR: Container "%CONTAINER_NAME%" is not running.
  echo Start it with: docker start %CONTAINER_NAME%
  pause
  exit /b 1
)

docker exec -i %CONTAINER_NAME% psql -U postgres -d terminology -c "SELECT 1;" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Database check failed.
  pause
  exit /b 1
)

echo.
echo ===========================================
echo Terminology Import
echo ===========================================
echo 1) Standard (Embeddings, no reindex)
echo 2) Vector (Embeddings + reindex)
echo Q) Quit
echo.

set /p choice=Select [1/2/Q]: 

if /i "%choice%"=="Q" goto :eof
if "%choice%"=="1" goto :standard
if "%choice%"=="2" goto :vector

echo Invalid choice.
pause
goto :eof

:activateVenv
if exist ".\.venv\Scripts\activate.bat" (
  call ".\.venv\Scripts\activate.bat"
  goto :eof
)
echo ERROR: .venv not found in %cd%
pause
exit /b 1

:ensurePkg
python -c "import %1" >nul 2>&1
if errorlevel 1 (
  echo Installing missing package: %1
  python -m pip install %1
  if errorlevel 1 exit /b 1
)
exit /b 0

:standard
call :activateVenv

call :ensurePkg openpyxl
call :ensurePkg psycopg
call :ensurePkg pgvector
call :ensurePkg sentence_transformers

echo.
echo Running import (batch embeddings, no reindex)...
python ".\scripts\setup_terminology.py" --batch 25
if errorlevel 1 (
  echo.
  echo ERROR: import failed.
  pause
  exit /b 1
)
echo.
echo Done.
pause
exit /b 0

:vector
call :activateVenv

call :ensurePkg openpyxl
call :ensurePkg psycopg
call :ensurePkg pgvector
call :ensurePkg sentence_transformers

echo.
echo Running import (batch embeddings + reindex)...
python ".\scripts\setup_terminology.py" --batch 25 --reindex
if errorlevel 1 (
  echo.
  echo ERROR: import failed.
  pause
  exit /b 1
)
echo.
echo Done.
pause
exit /b 0
