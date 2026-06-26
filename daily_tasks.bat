@echo off
REM 每日自动任务批处理脚本
REM 执行: 1) 分析采集产品 2) 库存同步

cd /d "%~dp0"

echo ============================================
echo Dajian Listing Tool - 每日任务
echo %date% %time%
echo ============================================

REM 激活虚拟环境
call .venv\Scripts\activate.bat

REM 设置 UTF-8 编码 (避免 emoji 输出报错)
set PYTHONIOENCODING=utf-8
chcp 65001 > nul 2>&1

REM 执行每日任务（包含分析 + 库存同步）
python daily_tasks.py

REM 记录完成时间
echo ============================================
echo 任务完成 %date% %time%
echo ============================================

REM 注意: 不要加 pause，否则任务计划程序会卡住不退出
