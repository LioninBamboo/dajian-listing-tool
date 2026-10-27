@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
echo ========================================
echo   Dajian Listing Tool - 启动服务
echo ========================================
echo.

cd /d "%~dp0"
set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "PYTHONW=%ROOT%.venv\Scripts\pythonw.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Missing venv python: "%PYTHON%"
    exit /b 1
)

REM ------------------------------------------
REM Resolve ports from this store's .env + profile
REM (no 8000/8501 fallback — abort on lookup fail)
REM ------------------------------------------
set "PORTS_FILE=%ROOT%logs\_store_ports.env"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
"%PYTHON%" -m src.utils.store_ports --format env > "%PORTS_FILE%" 2>"%ROOT%logs\_store_ports.err"
if errorlevel 1 (
    echo [ERROR] store_ports lookup failed
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
if not defined FASTAPI_TITLE set "FASTAPI_TITLE=FastAPI+Server"
if not defined STREAMLIT_TITLE set "STREAMLIT_TITLE=Streamlit+UI"
set "FASTAPI_TITLE=!FASTAPI_TITLE:+= !"
set "STREAMLIT_TITLE=!STREAMLIT_TITLE:+= !"
set "BRAND_NAME=!BRAND_NAME:+= !"
echo   Store: !BRAND_NAME!  FastAPI:!SERVER_PORT!  Streamlit:!STREAMLIT_PORT!
echo.

REM ------------------------------------------
REM 0. 清理残留进程 (避免端口占用 / 窗口堆积)
REM ------------------------------------------
echo [0] 清理残留任务窗口...
taskkill /F /FI "WINDOWTITLE eq Daily Tasks*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Title Optimizer*" >nul 2>&1
echo   OK
echo.

REM ------------------------------------------
REM 1. 核心服务 (ports from THIS store .env/profile)
REM ------------------------------------------
set SKIP_FASTAPI=0
netstat -ano | findstr ":!SERVER_PORT! " | findstr LISTENING >nul
if %ERRORLEVEL%==0 (
    echo [1/4] FastAPI 已在运行 ^(端口 !SERVER_PORT!^)，跳过
    set SKIP_FASTAPI=1
)
if "%SKIP_FASTAPI%"=="0" (
    echo [1/4] 启动 FastAPI 服务 ^(端口 !SERVER_PORT!^)...
    start "!FASTAPI_TITLE!" /min cmd /c "cd /d %ROOT% && %PYTHON% server.py"
    ping 127.0.0.1 -n 4 >nul
)

set SKIP_STREAMLIT=0
netstat -ano | findstr ":!STREAMLIT_PORT! " | findstr LISTENING >nul
if %ERRORLEVEL%==0 (
    echo [2/4] Streamlit 已在运行 ^(端口 !STREAMLIT_PORT!^)，跳过
    set SKIP_STREAMLIT=1
)
if "%SKIP_STREAMLIT%"=="0" (
    echo [2/4] 启动 Streamlit 界面 ^(端口 !STREAMLIT_PORT!^)...
    start "!STREAMLIT_TITLE!" /min cmd /c "cd /d %ROOT% && %PYTHON% -m streamlit run app.py --server.port !STREAMLIT_PORT!"
)

REM ------------------------------------------
REM 2. 后台调度守护进程 (替代旧的一次性任务窗口)
REM ------------------------------------------
set SKIP_SCHEDULER=0
if exist "%ROOT%logs\_scheduler.pid" (
    set /p OLD_PID=<"%ROOT%logs\_scheduler.pid"
    tasklist /NH 2>nul | findstr /C:"!OLD_PID!" >nul
    if !ERRORLEVEL!==0 (
        echo [3/4] 调度守护进程已在运行，跳过
        set SKIP_SCHEDULER=1
    ) else (
        del "%ROOT%logs\_scheduler.pid" >nul 2>&1
    )
)
if "%SKIP_SCHEDULER%"=="0" (
    echo [3/4] 启动调度守护进程 ^(后台无窗口^)...
    if exist "%PYTHONW%" (
        start "" /b "%PYTHONW%" "%ROOT%scheduler_daemon.py"
        echo   pythonw.exe 模式 — 完全无窗口
    ) else (
        start "Scheduler Daemon - !BRAND_NAME!" /min cmd /c "cd /d %ROOT% && %PYTHON% scheduler_daemon.py"
        echo   最小化窗口模式
    )
)

REM ------------------------------------------
REM 3.5 看门狗自检与一次性触发
REM ------------------------------------------
echo [4/4] 看门狗自检...
schtasks /query /tn "Dajian Scheduler Watchdog" >nul 2>&1
if %ERRORLEVEL%==0 (
    echo   Watchdog 计划任务已注册
) else (
    echo   [WARN] Watchdog 未注册，请右键管理员运行 setup_scheduled_tasks.bat
)

if exist "%PYTHONW%" (
    start "" /b "%PYTHONW%" "%ROOT%scheduler_watchdog.py"
) else (
    start "Watchdog Check" /min cmd /c "cd /d %ROOT% && %PYTHON% scheduler_watchdog.py"
)
echo   已执行一次看门狗检查

echo.
echo ========================================
echo   服务已启动！
echo   - FastAPI:    http://localhost:!SERVER_PORT!
echo   - Streamlit:  http://localhost:!STREAMLIT_PORT!
echo   - 调度器:     后台运行中
echo.
echo   仅 Web ^(不含调度^): start_web.bat
echo   查看端口: check_services.bat
echo   停止服务: stop.bat
echo ========================================
echo.
echo 3 秒后自动打开 Streamlit 界面...
ping 127.0.0.1 -n 4 >nul

start http://localhost:!STREAMLIT_PORT!
exit
