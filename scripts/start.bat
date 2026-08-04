@echo off
REM Windows 启动脚本。
REM   start.bat          前台运行
REM   start.bat bg       后台运行（pythonw，无控制台窗口）
REM
REM 停止：任务管理器结束 python 进程，或 taskkill /IM python.exe
REM 单实例保护靠端口绑定失败，不用 PID 文件。
setlocal
cd /d "%~dp0.."

set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

if not exist ".venv\Scripts\python.exe" (
    echo 未找到 .venv，请先运行：python scripts\setup.py
    exit /b 1
)

if /i "%~1"=="bg" (
    echo 后台启动 POE1-Agent...
    start "" ".venv\Scripts\pythonw.exe" main.py
) else (
    ".venv\Scripts\python.exe" main.py
)
