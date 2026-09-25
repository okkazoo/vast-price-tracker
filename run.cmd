@echo off
rem Hourly entry point: collect a snapshot, then re-render the charts (even if collect failed).
cd /d "%~dp0"
if not exist data mkdir data
echo ===== %DATE% %TIME% ===== >> data\run.log
python collect.py >> data\run.log 2>&1
echo collect exit %ERRORLEVEL% >> data\run.log
python chart.py >> data\run.log 2>&1
