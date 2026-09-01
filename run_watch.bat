@echo off
REM A-Trade watch loop for Windows (Task Scheduler or double-click).
REM Runs the open (09:25 ET) and close (15:50 ET) sessions on weekdays.
cd /d %~dp0
python -m atrade.cli watch --interval 60
