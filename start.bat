@echo off
chcp 65001 >nul
echo ========================================
echo   Dajian Listing Tool - 启动服务
echo ========================================
echo.

cd /d "%~dp0"

:: 启动 FastAPI (后台)
echo [1/2] 启动 FastAPI 服务 (端口 8000)...
start "FastAPI" /min cmd /c "cd /d "%~dp0" && ".venv\Scripts\python.exe" server.py"

:: 等待 3 秒
ping 127.0.0.1 -n 4 >nul

:: 启动 Streamlit
echo [2/2] 启动 Streamlit 界面 (端口 8501)...
start "Streamlit" cmd /c "cd /d "%~dp0" && ".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501"

echo.
echo ========================================
echo   服务已启动！
echo   - FastAPI: http://localhost:8000
echo   - Streamlit: http://localhost:8501
echo ========================================
echo.
echo 3 秒后自动打开 Streamlit 界面...
ping 127.0.0.1 -n 4 >nul

start http://localhost:8501
exit
