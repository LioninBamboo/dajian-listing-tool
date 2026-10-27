@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
echo ========================================
echo   Dajian Listing Tool - 仅启动 Web
echo   (FastAPI + Streamlit, 不含调度守护)
echo ========================================
echo.

cd /d "%~dp0"
set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Missing venv python: "%PYTHON%"
    exit /b 1
)

set "SERVER_PORT="
set "STREAMLIT_PORT="
set "FASTAPI_TITLE="
set "STREAMLIT_TITLE="
set "BRAND_NAME="
for /f "usebackq tokens=1,* delims==" %%A in (`"%PYTHON%" -m src.utils.store_ports --format env`) do (
    if /I "%%A"=="SERVER_PORT" set "SERVER_PORT=%%B"
    if /I "%%A"=="STREAMLIT_PORT" set "STREAMLIT_PORT=%%B"
    if /I "%%A"=="FASTAPI_WINDOW_TITLE" set "FASTAPI_TITLE=%%B"
    if /I "%%A"=="STREAMLIT_WINDOW_TITLE" set "STREAMLIT_TITLE=%%B"
    if /I "%%A"=="BRAND_NAME" set "BRAND_NAME=%%B"
)
if not defined SERVER_PORT set "SERVER_PORT=8000"
if not defined STREAMLIT_PORT set "STREAMLIT_PORT=8501"
if not defined FASTAPI_TITLE set "FASTAPI_TITLE=FastAPI+Server"
if not defined STREAMLIT_TITLE set "STREAMLIT_TITLE=Streamlit+UI"
set "FASTAPI_TITLE=!FASTAPI_TITLE:+= !"
set "STREAMLIT_TITLE=!STREAMLIT_TITLE:+= !"
set "BRAND_NAME=!BRAND_NAME:+= !"
echo   Store: !BRAND_NAME!  FastAPI:!SERVER_PORT!  Streamlit:!STREAMLIT_PORT!
echo.

set SKIP_FASTAPI=0
netstat -ano | findstr ":!SERVER_PORT! " | findstr LISTENING >nul
if %ERRORLEVEL%==0 (
    echo [1/2] FastAPI 已在运行 ^(端口 !SERVER_PORT!^)，跳过
    set SKIP_FASTAPI=1
)
if "%SKIP_FASTAPI%"=="0" (
    echo [1/2] 启动 FastAPI 服务 ^(端口 !SERVER_PORT!^)...
    start "!FASTAPI_TITLE!" /min cmd /c "cd /d %ROOT% && %PYTHON% server.py"
    ping 127.0.0.1 -n 4 >nul
)

set SKIP_STREAMLIT=0
netstat -ano | findstr ":!STREAMLIT_PORT! " | findstr LISTENING >nul
if %ERRORLEVEL%==0 (
    echo [2/2] Streamlit 已在运行 ^(端口 !STREAMLIT_PORT!^)，跳过
    set SKIP_STREAMLIT=1
)
if "%SKIP_STREAMLIT%"=="0" (
    echo [2/2] 启动 Streamlit 界面 ^(端口 !STREAMLIT_PORT!^)...
    start "!STREAMLIT_TITLE!" /min cmd /c "cd /d %ROOT% && %PYTHON% -m streamlit run app.py --server.port !STREAMLIT_PORT!"
)

echo.
echo ========================================
echo   Web 服务就绪 ^(未启动调度守护进程^)
echo   - FastAPI:   http://localhost:!SERVER_PORT!
echo   - Streamlit: http://localhost:!STREAMLIT_PORT!
echo   登录自启注册: setup_web_services.bat ^(管理员^)
echo ========================================
exit /b 0
