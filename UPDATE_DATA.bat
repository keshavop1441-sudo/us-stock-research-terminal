@echo off
rem Synchronizes market data into data\research.duckdb.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Run START_TERMINAL.bat once first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" scripts\update_data.py
set EXITCODE=%ERRORLEVEL%
pause
exit /b %EXITCODE%
