@echo off
setlocal
cd /d "%~dp0"
title Configure Windows Task Scheduler for Network Monitor
chcp 65001 >nul 2>&1

echo ======================================================
echo   Configure Task Scheduler for Network Monitor
echo   (Optimized for 24/7 background operation)
echo ======================================================
echo.

set "TASK_NAME=NetworkSpeedMonitor"
set "SCRIPT_PATH=%~dp0start_monitor.bat"
set "WORKDIR=%~dp0"

echo [info] Task Name: %TASK_NAME%
echo [info] Target:    %SCRIPT_PATH%
echo [info] WorkDir:   %WORKDIR%
echo.

REM Remove existing task if any
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

REM Create task with unlimited runtime (never stops after 3 days) and explicit WorkingDirectory
powershell -NoProfile -Command "$action = New-ScheduledTaskAction -Execute '%SCRIPT_PATH%' -WorkingDirectory '%WORKDIR%'; $trigger = New-ScheduledTaskTrigger -AtLogOn; $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0) -Priority 5; Register-ScheduledTask -TaskName '%TASK_NAME%' -Action $action -Trigger $trigger -Settings $settings -Description 'Network and Power Speed Monitor 24/7 Daemon' -Force"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ======================================================
    echo [SUCCESS] Task '%TASK_NAME%' created successfully!
    echo - Trigger: At User Logon
    echo - Execution Limit: Never stops (runs 24/7)
    echo - Working Directory: Set correctly to monitor folder
    echo ======================================================
    echo.
    echo Starting the task now...
    schtasks /run /tn "%TASK_NAME%"
    echo [OK] Task started. You can check the dashboard or run 'check_status.bat'.
) else (
    echo.
    echo ======================================================
    echo [NOTICE] PowerShell registration returned %ERRORLEVEL%.
    echo Falling back to standard schtasks creation...
    echo ======================================================
    schtasks /create /tn "%TASK_NAME%" /tr ""%SCRIPT_PATH%"" /sc onlogon /rl highest /f
    schtasks /run /tn "%TASK_NAME%"
)

echo.
pause
