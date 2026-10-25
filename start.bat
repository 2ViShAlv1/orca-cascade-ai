@echo off
rem One-click launcher for Windows: starts ORCA and opens the dashboard.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
start "" http://127.0.0.1:8000
python run.py %*
pause
