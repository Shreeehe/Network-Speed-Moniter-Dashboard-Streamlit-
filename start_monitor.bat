@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Network and Power Speed Monitor

chcp 65001 >nul
set PYTHONIOENCODING=utf-8

REM Disable QuickEdit mode so console mouse clicks do not freeze execution
reg add HKCU\Console /v QuickEdit /t REG_DWORD /d 0 /f >nul 2>&1

REM Ensure logs directory exists
if not exist logs mkdir logs

echo ======================================================
echo       Network and Power Speed Monitor
echo ======================================================
echo [info] Location: %cd%
echo [info] Log file: logs\network_monitor.log
echo [info] Launcher log: logs\bat_launch.log
echo ======================================================
echo.

REM Detect Python executable
set "PY_CMD="
if exist ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
    echo [info] Detected virtual environment: .venv\Scripts\python.exe
) else (
    where python >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
        set "PY_CMD=python"
        echo [info] Detected system Python
    ) else (
        where py >nul 2>nul
        if !ERRORLEVEL! EQU 0 (
            set "PY_CMD=py"
            echo [info] Detected Python Launcher (py)
        ) else (
            where uv >nul 2>nul
            if !ERRORLEVEL! EQU 0 (
                set "PY_CMD=uv run"
                echo [info] Detected uv runner
            )
        )
    )
)

if "!PY_CMD!"=="" (
    echo.
    echo ======================================================
    echo [ERROR] Python was not found on this system!
    echo Please install Python 3.9+ or run with virtualenv.
    echo ======================================================
    echo [%date% %time%] ERROR: Python not found >> logs\bat_launch.log
    pause
    exit /b 1
)

REM Record launch timestamp
echo [%date% %time%] Batch launcher started with !PY_CMD! >> logs\bat_launch.log

REM Initial countdown on manual start (press any key to start immediately)
echo [info] Starting monitor in 5 seconds (press any key to start immediately)...
timeout /t 5 >nul

:loop
cls
echo ======================================================
echo       Network and Power Speed Monitor
echo ======================================================
echo [info] Started at: %date% %time%
echo [info] Executable: !PY_CMD!
echo [info] Running network_monitor.py...
echo ======================================================
echo.

echo [%date% %time%] Launching network_monitor.py >> logs\bat_launch.log
!PY_CMD! network_monitor.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ======================================================
echo [%date% %time%] Monitor stopped with exit code: !EXIT_CODE!
echo [%date% %time%] Process stopped with exit code: !EXIT_CODE! >> logs\bat_launch.log
echo ======================================================
echo.
echo [AUTO-RESTART] Restarting monitor in 10 seconds...
echo (Press Ctrl+C to cancel auto-restart and terminate)
echo ======================================================
timeout /t 10
goto loop
