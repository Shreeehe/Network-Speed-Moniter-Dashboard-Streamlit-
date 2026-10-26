@echo off
cd /d "%~dp0"
title Remove Network Monitor Auto-Start
chcp 65001 >nul 2>&1

echo ======================================================
echo   Removing Network Monitor Auto-Start
echo ======================================================
echo.

set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\NetworkMonitor.lnk"
if exist "%SHORTCUT%" (
    del "%SHORTCUT%"
    echo [OK] Removed Startup folder shortcut.
)

schtasks /delete /tn "NetworkSpeedMonitor" /f >nul 2>&1
echo [OK] Removed Task Scheduler task (if registered).

echo.
echo Stopping any running monitor processes...
call "%~dp0stop_monitor.bat"
