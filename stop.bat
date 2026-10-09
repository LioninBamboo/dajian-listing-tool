@echo off
chcp 65001 >nul
echo 正在停止 Dajian Listing Tool 所有服务...

cd /d "%~dp0"
set "ROOT=%~dp0"

:: ──────────────────────────────────────────
:: 1. 停止调度守护进程
:: ──────────────────────────────────────────
if exist "%ROOT%logs\_scheduler.pid" (
    set /p SCHED_PID=<"%ROOT%logs\_scheduler.pid"
    echo 停止调度守护进程 (PID=%SCHED_PID%)...
    taskkill /F /PID %SCHED_PID% >nul 2>&1
    del "%ROOT%logs\_scheduler.pid" >nul 2>&1
    echo   OK
) else (
    echo 调度守护进程未运行
)

:: ──────────────────────────────────────────
:: 2. 停止服务窗口 (按标题)
:: ──────────────────────────────────────────
:: 停止 Streamlit（按窗口标题）
taskkill /F /FI "WINDOWTITLE eq Streamlit UI*" >nul 2>&1

:: 停止 FastAPI（按窗口标题）
taskkill /F /FI "WINDOWTITLE eq FastAPI Server*" >nul 2>&1

:: 停止旧版任务窗口（如果有）
taskkill /F /FI "WINDOWTITLE eq Daily Tasks*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Title Optimizer*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Scheduler Daemon*" >nul 2>&1

:: ──────────────────────────────────────────
:: 3. 按端口清理残留进程
:: ──────────────────────────────────────────
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8501 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8502 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

echo.
echo ========================================
echo   所有服务已停止
echo   - FastAPI Server
echo   - Streamlit UI
echo   - 调度守护进程
echo ========================================
pause
