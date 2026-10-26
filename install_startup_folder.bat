@echo off
setlocal
cd /d "%~dp0"
title Add Network Monitor to Windows Startup
chcp 65001 >nul 2>&1

echo ======================================================
echo   Add Network Monitor to Windows Startup Folder
echo   (Recommended: Best for 4GB RAM i3 PC)
echo ======================================================
echo.

set "TARGET=%~dp0start_monitor.bat"
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\NetworkMonitor.lnk"

powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'Network Monitor'; $s.Save()"

if exist "%SHORTCUT%" (
    echo [SUCCESS] Startup shortcut created!
    echo Location: %SHORTCUT%
    echo.
    echo The monitor will launch automatically whenever this PC turns on or logs in.
    echo.
    echo Starting the monitor now...
    start "" "%TARGET%"
) else (
    echo [ERROR] Could not create startup shortcut.
)
echo.
pause
