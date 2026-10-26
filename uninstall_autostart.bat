@echo off
setlocal enabledelayedexpansion
title Uninstall Network Monitor Auto-Start
chcp 65001 >nul
cd /d "%~dp0"

echo ======================================================
echo    Remove Network Monitor from Windows Auto-Start
echo ======================================================
echo.

set "SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\NetworkMonitor.lnk"

if exist "%SHORTCUT_PATH%" (
    del "%SHORTCUT_PATH%"
    echo [OK] Auto-start shortcut removed from Windows Startup folder.
) else (
    echo [INFO] No auto-start shortcut was found in Startup folder.
)

echo.
echo Stopping any running monitor processes...
call "%~dp0stop_monitor.bat"
