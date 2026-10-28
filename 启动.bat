@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo 找不到 Python 3.10 或更高版本，请先安装：https://www.python.org/downloads/
    pause
    exit /b 1
  )
  python 启动.py
) else (
  py -3 启动.py
)
if errorlevel 1 pause
