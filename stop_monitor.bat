@echo off
title Stop Network Monitor
echo ========================================
echo   Stopping Network ^& Power Monitor...
echo ========================================
echo.

powershell -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*network_monitor*' -and $_.ProcessId -ne $PID }; if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('[OK] Stopped Network Monitor process (PID ' + $_.ProcessId + ')') } } else { Write-Host '[INFO] No running Network Monitor process found.' }"

echo.
echo Done. Press any key to exit...
pause >nul
