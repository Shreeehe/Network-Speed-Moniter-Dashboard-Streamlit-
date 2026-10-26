@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Install Network Monitor Auto-Start
chcp 65001 >nul

echo ======================================================
echo    Install Network Monitor to Windows Auto-Start
echo ======================================================
echo.

set "TARGET_SCRIPT=%~dp0start_monitor_hidden.vbs"
set "SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\NetworkMonitor.lnk"

powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = '%TARGET_SCRIPT%'; $s.WorkingDirectory = '%~dp0'; $s.Save()"

if exist "%SHORTCUT_PATH%" (
    echo [SUCCESS] Auto-start shortcut created successfully!
    echo [Target] %SHORTCUT_PATH%
    echo.
    echo The monitor will now automatically start on every Windows boot / login.
    echo.
    echo [info] Launching background monitor process now...
    cscript //nologo "%TARGET_SCRIPT%"
    echo [OK] Monitor launched in background.
    echo You can use 'check_status.bat' anytime to verify that it is running.
) else (
    echo [ERROR] Failed to create startup shortcut.
)

echo.
echo ======================================================
pause
