@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Missing venv python: "%PYTHON%"
    exit /b 1
)

set "SERVER_PORT="
set "STREAMLIT_PORT="
set "BRAND_NAME="
for /f "usebackq tokens=1,* delims==" %%A in (`"%PYTHON%" -m src.utils.store_ports --format env`) do (
    if /I "%%A"=="SERVER_PORT" set "SERVER_PORT=%%B"
    if /I "%%A"=="STREAMLIT_PORT" set "STREAMLIT_PORT=%%B"
    if /I "%%A"=="BRAND_NAME" set "BRAND_NAME=%%B"
)
if not defined SERVER_PORT set "SERVER_PORT=8000"
if not defined STREAMLIT_PORT set "STREAMLIT_PORT=8501"
set "BRAND_NAME=!BRAND_NAME:+= !"

echo ========================================
echo   服务状态检查 - !BRAND_NAME!
echo ========================================
echo   Profile FastAPI port:    !SERVER_PORT!
echo   Derived Streamlit port:  !STREAMLIT_PORT!
echo.

set "API_UP=0"
set "UI_UP=0"
set "SCHED_UP=0"

netstat -ano | findstr ":!SERVER_PORT! " | findstr LISTENING >nul
if %ERRORLEVEL%==0 (
    set "API_UP=1"
    echo   FastAPI   :!SERVER_PORT!   LISTENING
) else (
    echo   FastAPI   :!SERVER_PORT!   DOWN
)

netstat -ano | findstr ":!STREAMLIT_PORT! " | findstr LISTENING >nul
if %ERRORLEVEL%==0 (
    set "UI_UP=1"
    echo   Streamlit :!STREAMLIT_PORT!   LISTENING
) else (
    echo   Streamlit :!STREAMLIT_PORT!   DOWN
)

if exist "%ROOT%logs\_scheduler.pid" (
    set /p SCHED_PID=<"%ROOT%logs\_scheduler.pid"
    tasklist /NH 2>nul | findstr /C:"!SCHED_PID!" >nul
    if !ERRORLEVEL!==0 (
        set "SCHED_UP=1"
        echo   Scheduler PID=!SCHED_PID!  ALIVE
    ) else (
        echo   Scheduler PID=!SCHED_PID!  STALE pidfile
    )
) else (
    echo   Scheduler            not running
)

echo.
if "!API_UP!!UI_UP!"=="11" (
    echo   Web services: OK
    exit /b 0
) else (
    echo   Web services: incomplete — run start_web.bat or start.bat
    exit /b 1
)
