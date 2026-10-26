@echo off
title Network Monitor Status Checker
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ======================================================
echo       Network Monitor Status Checker
echo ======================================================
echo.

powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*network_monitor.py*' -and $_.ProcessId -ne $PID }; if ($procs) { Write-Host ' [STATUS: ACTIVE AND RUNNING]' -ForegroundColor Green; foreach ($p in $procs) { Write-Host ('   - Process ID: ' + $p.ProcessId) } } else { Write-Host ' [STATUS: NOT RUNNING]' -ForegroundColor Red; Write-Host '   No active network_monitor process found.' }"

echo.
echo ======================================================
echo Recent Activity (last 10 lines from logs\network_monitor.log):
echo ======================================================
if exist "logs\network_monitor.log" (
    powershell -NoProfile -Command "Get-Content -Path 'logs\network_monitor.log' -Tail 10 -Encoding UTF8 -ErrorAction SilentlyContinue"
) else (
    echo [info] No logs\network_monitor.log found yet.
)
echo.
echo ======================================================
pause
