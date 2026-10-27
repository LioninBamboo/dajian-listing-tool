@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
echo 正在停止本店 Dajian Listing Tool 服务...

cd /d "%~dp0"
set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Missing venv python: "%PYTHON%"
    exit /b 1
)

set "PORTS_FILE=%ROOT%logs\_store_ports.env"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
"%PYTHON%" -m src.utils.store_ports --format env > "%PORTS_FILE%" 2>"%ROOT%logs\_store_ports.err"
if errorlevel 1 (
    echo [ERROR] store_ports lookup failed — refusing to stop with guessed ports
    type "%ROOT%logs\_store_ports.err" 2>nul
    exit /b 1
)
set "SERVER_PORT="
set "STREAMLIT_PORT="
set "FASTAPI_TITLE="
set "STREAMLIT_TITLE="
set "BRAND_NAME="
for /f "usebackq tokens=1,* delims==" %%A in ("%PORTS_FILE%") do (
    if /I "%%A"=="SERVER_PORT" set "SERVER_PORT=%%B"
    if /I "%%A"=="STREAMLIT_PORT" set "STREAMLIT_PORT=%%B"
    if /I "%%A"=="FASTAPI_WINDOW_TITLE" set "FASTAPI_TITLE=%%B"
    if /I "%%A"=="STREAMLIT_WINDOW_TITLE" set "STREAMLIT_TITLE=%%B"
    if /I "%%A"=="BRAND_NAME" set "BRAND_NAME=%%B"
)
if not defined SERVER_PORT (
    echo [ERROR] store_ports printed no SERVER_PORT
    exit /b 1
)
if not defined STREAMLIT_PORT (
    echo [ERROR] store_ports printed no STREAMLIT_PORT
    exit /b 1
)
set "FASTAPI_TITLE=!FASTAPI_TITLE:+= !"
set "STREAMLIT_TITLE=!STREAMLIT_TITLE:+= !"
set "BRAND_NAME=!BRAND_NAME:+= !"

REM Refuse misconfigured non-Main clones that resolved onto Main default ports.
if /I not "!BRAND_NAME!"=="AquaVerve" (
    if "!SERVER_PORT!"=="8000" (
        echo [ERROR] Refusing stop: brand=!BRAND_NAME! resolved to Main FastAPI port 8000
        exit /b 1
    )
    if "!STREAMLIT_PORT!"=="8501" (
        echo [ERROR] Refusing stop: brand=!BRAND_NAME! resolved to Main Streamlit port 8501
        exit /b 1
    )
)

echo   Store: !BRAND_NAME!  FastAPI:!SERVER_PORT!  Streamlit:!STREAMLIT_PORT!

:: ──────────────────────────────────────────
:: 1. 停止调度守护进程
:: ──────────────────────────────────────────
if exist "%ROOT%logs\_scheduler.pid" (
    set /p SCHED_PID=<"%ROOT%logs\_scheduler.pid"
    echo 停止调度守护进程 (PID=!SCHED_PID!)...
    taskkill /F /PID !SCHED_PID! >nul 2>&1
    del "%ROOT%logs\_scheduler.pid" >nul 2>&1
    echo   OK
) else (
    echo 调度守护进程未运行
)

:: ──────────────────────────────────────────
:: 2. 停止本店服务窗口 (仅店铺专属标题；勿用
::    "Streamlit UI*" 通配，会误杀其他店)
:: ──────────────────────────────────────────
if defined STREAMLIT_TITLE (
    taskkill /F /FI "WINDOWTITLE eq !STREAMLIT_TITLE!*" >nul 2>&1
)
if defined FASTAPI_TITLE (
    taskkill /F /FI "WINDOWTITLE eq !FASTAPI_TITLE!*" >nul 2>&1
)
if defined BRAND_NAME (
    taskkill /F /FI "WINDOWTITLE eq Scheduler Daemon - !BRAND_NAME!*" >nul 2>&1
)
taskkill /F /FI "WINDOWTITLE eq Daily Tasks*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Title Optimizer*" >nul 2>&1

:: ──────────────────────────────────────────
:: 3. 仅按本店端口清理残留 (不碰其他店)
:: ──────────────────────────────────────────
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":!SERVER_PORT! " ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":!STREAMLIT_PORT! " ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

echo.
echo ========================================
echo   本店服务已停止
echo   - FastAPI :!SERVER_PORT!
echo   - Streamlit :!STREAMLIT_PORT!
echo   - 调度守护进程
echo ========================================
pause
