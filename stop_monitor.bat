@echo off
title Stop Network Monitor
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ======================================================
echo       Stopping Network and Power Monitor
echo ======================================================
echo.

powershell -NoProfile -Command "$stopped = 0; $procs = Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -like '*network_monitor.py*' -or $_.CommandLine -like '*start_monitor.bat*') -and $_.ProcessId -ne $PID }; foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Host (' [OK] Stopped ' + $p.Name + ' (PID ' + $p.ProcessId + ')') -ForegroundColor Green; $stopped++ } catch {} }; if ($stopped -eq 0) { Write-Host ' [INFO] No running monitor processes found.' -ForegroundColor Yellow }"

echo.
echo ======================================================
echo Done. Press any key to exit...
pause >nul
