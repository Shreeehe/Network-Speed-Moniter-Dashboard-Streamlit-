@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Network & Power Monitor

chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo ======================================================
echo       ⚡ Network and Power Speed Monitor
echo ======================================================
echo [info] Location: %cd%
echo [info] Logs are saved to: logs\network_monitor.log
echo.

REM Ensure logs directory exists
if not exist logs mkdir logs

REM Record launch timestamp
echo [%date% %time%] Batch script started >> logs\bat_launch.log

REM 5 second countdown (press any key to start immediately)
echo [info] Starting monitor in 5 seconds (press any key to start now)...
timeout /t 5

echo.
echo [info] Launching network_monitor.py...
echo ======================================================
echo.

if exist ".venv\Scripts\python.exe" (
    echo [info] Using virtualenv Python: .venv\Scripts\python.exe
    ".venv\Scripts\python.exe" network_monitor.py
) else (
    where uv >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
        echo [info] Using uv runner
        uv run network_monitor.py
    ) else (
        echo [info] Using system Python
        python network_monitor.py
    )
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ======================================================
    echo [ERROR] Monitor exited with error code %ERRORLEVEL%.
    echo Please check logs\network_monitor.log for details.
    echo ======================================================
    pause
)
