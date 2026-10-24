@echo off
REM Change to the directory of this batch file
cd /d "%~dp0"

REM Ensure logs folder exists
if not exist logs mkdir logs

REM Record launch event
echo [%date% %time%] Batch script launched, waiting 60s for network... >> logs\bat_launch.log

REM Wait for Windows/WiFi/Router to come up after a power cut before testing
timeout /t 60 /nobreak >nul

echo [%date% %time%] Starting network_monitor.py... >> logs\bat_launch.log

REM Run python monitor and redirect both stdout and stderr to logs/bat_output.log
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe network_monitor.py >> logs\bat_output.log 2>&1
) else (
    python network_monitor.py >> logs\bat_output.log 2>&1
)
