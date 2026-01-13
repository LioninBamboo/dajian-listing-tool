@echo off
echo 正在停止 Dajian Listing Tool 服务...

:: 停止 Streamlit
taskkill /F /FI "WINDOWTITLE eq Streamlit UI*" >nul 2>&1

:: 停止 FastAPI
taskkill /F /FI "WINDOWTITLE eq FastAPI Server*" >nul 2>&1

:: 也可以按端口停止
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8501 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

echo 服务已停止。
pause
