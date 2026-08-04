@echo off
REM 薄封装：真正的初始化逻辑在 scripts\setup.py（跨平台唯一真源）。
REM 新增初始化步骤请改 setup.py，不要改这里。
setlocal
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo 未找到 python，请安装 python.org 官方 Python 3.10+
    echo 注意：不要用 Microsoft Store 版，它有路径重定向问题
    exit /b 1
)

python scripts\setup.py %*
