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

REM ------------------------------------------
REM 0. 清理残留进程 (避免端口占用 / 窗口堆积)
REM ------------------------------------------
echo [0] 清理残留任务窗口...
taskkill /F /FI "WINDOWTITLE eq Daily Tasks*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Title Optimizer*" >nul 2>&1
echo   OK
echo.

REM ------------------------------------------
REM 1. 核心服务
REM ------------------------------------------
:: 检查 FastAPI 是否已在运行
set SKIP_FASTAPI=0
netstat -ano | findstr :8000 | findstr LISTENING >nul
if %ERRORLEVEL%==0 (
    echo [1/4] FastAPI 已在运行，跳过
    set SKIP_FASTAPI=1
)
if "%SKIP_FASTAPI%"=="0" (
    echo [1/4] 启动 FastAPI 服务 ^(端口 8000^)...
    start "FastAPI Server" /min cmd /c "cd /d %ROOT% && %PYTHON% server.py"
    ping 127.0.0.1 -n 4 >nul
)

:: 检查 Streamlit 是否已在运行
set STREAMLIT_PORT=8501
set SKIP_STREAMLIT=0
netstat -ano | findstr :8501 | findstr LISTENING >nul
if %ERRORLEVEL%==0 (
    echo [2/4] Streamlit 已在运行，跳过
    set SKIP_STREAMLIT=1
)
if "%SKIP_STREAMLIT%"=="0" (
    echo [2/4] 启动 Streamlit 界面 ^(端口 %STREAMLIT_PORT%^)...
    start "Streamlit UI" /min cmd /c "cd /d %ROOT% && %PYTHON% -m streamlit run app.py --server.port %STREAMLIT_PORT%"
)

REM ------------------------------------------
REM 2. 后台调度守护进程 (替代旧的一次性任务窗口)
REM ------------------------------------------
set SKIP_SCHEDULER=0
if exist "%ROOT%logs\_scheduler.pid" (
    set /p OLD_PID=<"%ROOT%logs\_scheduler.pid"
    tasklist /NH 2>nul | findstr /C:"%OLD_PID%" >nul
    if !ERRORLEVEL!==0 (
        echo [3/3] 调度守护进程已在运行，跳过
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
        start "Scheduler Daemon" /min cmd /c "cd /d %ROOT% && %PYTHON% scheduler_daemon.py"
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
echo   - FastAPI:    http://localhost:8000
echo   - Streamlit:  http://localhost:%STREAMLIT_PORT%
echo   - 调度器:     后台运行中
echo.
echo   任务时间表:
echo     09:00  标题优化 (50个/批)
echo     09:30  每日全量任务 (含库存同步+报告)
echo     每 2h  自动分析
echo.
echo   查看状态: python scheduler_daemon.py --status
echo   停止服务: stop.bat
echo ========================================
echo.
echo 3 秒后自动打开 Streamlit 界面...
ping 127.0.0.1 -n 4 >nul

start http://localhost:%STREAMLIT_PORT%
exit
