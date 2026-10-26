@echo off
cd /d "%~dp0"
title Network and Power Speed Monitor

chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8

REM Ensure logs directory exists
if not exist logs mkdir logs

echo ======================================================
echo       Network and Power Speed Monitor
echo ======================================================
echo [info] Folder: %cd%
echo [info] Log file: logs\network_monitor.log
echo [info] Launcher log: logs\bat_launch.log
echo ======================================================
echo.

REM 1. Detect Python executable across all standard paths
set "PY_CMD="

REM Check virtualenv inside monitor folder
if exist "%~dp0.venv\Scripts\python.exe" set "PY_CMD=%~dp0.venv\Scripts\python.exe"

REM Check standard python command
if not defined PY_CMD (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

REM Check Python Launcher (py)
if not defined PY_CMD (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)

REM Check where python points to
if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

REM Check common Windows Python install locations (32-bit & 64-bit on older PCs)
if not defined PY_CMD (
    for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PY_CMD=%%D\python.exe"
    )
)
if not defined PY_CMD (
    for /d %%D in ("C:\Python3*") do (
        if exist "%%D\python.exe" set "PY_CMD=%%D\python.exe"
    )
)
if not defined PY_CMD (
    for /d %%D in ("C:\Program Files\Python3*") do (
        if exist "%%D\python.exe" set "PY_CMD=%%D\python.exe"
    )
)
if not defined PY_CMD (
    for /d %%D in ("C:\Program Files (x86)\Python3*") do (
        if exist "%%D\python.exe" set "PY_CMD=%%D\python.exe"
    )
)

if not defined PY_CMD (
    echo.
    echo ======================================================
    echo [ERROR] Python was not found on this computer!
    echo.
    echo Please install Python 3.9+ from python.org
    echo IMPORTANT: Make sure to check "Add Python to PATH"
    echo during installation!
    echo ======================================================
    echo [%date% %time%] ERROR: Python executable not found >> logs\bat_launch.log
    pause
    exit /b 1
)

echo [info] Using Python: %PY_CMD%
echo [%date% %time%] Launcher started with %PY_CMD% >> logs\bat_launch.log
echo [info] Starting monitor in 3 seconds...
REM ping delay works in all Windows versions, background tasks, and Task Scheduler
ping 127.0.0.1 -n 4 >nul

:loop
echo.
echo ======================================================
echo [%date% %time%] Starting network_monitor.py...
echo ======================================================
echo [%date% %time%] Launching network_monitor.py >> logs\bat_launch.log

%PY_CMD% network_monitor.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ======================================================
echo [%date% %time%] Monitor stopped (exit code: %EXIT_CODE%)
echo [%date% %time%] Process stopped with exit code: %EXIT_CODE% >> logs\bat_launch.log
echo ======================================================
echo.
echo [AUTO-RESTART] Restarting in 10 seconds...
echo (Press Ctrl+C to stop the monitor)
echo ======================================================
ping 127.0.0.1 -n 11 >nul
goto loop
