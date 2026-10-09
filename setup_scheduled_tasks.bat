@echo off
chcp 65001 >nul
cd /d "%~dp0"
for %%I in ("%~dp0.") do set "ROOT=%%~fI"
set "REGISTER_PS1=%ROOT%\scripts\register_scheduled_tasks.ps1"

if not exist "%REGISTER_PS1%" (
    echo Missing script: "%REGISTER_PS1%"
    pause
    exit /b 1
)

net session >nul 2>&1
if not "%ERRORLEVEL%"=="0" (
    echo Requesting administrator rights...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b %ERRORLEVEL%
)

echo Running scheduled task installer as administrator...
powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%REGISTER_PS1%" -ProjectRoot "%ROOT%" -LogonType S4U
exit /b %ERRORLEVEL%
