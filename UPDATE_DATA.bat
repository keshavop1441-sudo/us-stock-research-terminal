@echo off
rem Synchronizes market data into data\research.duckdb. Only one refresh can run at a time.
rem   UPDATE_DATA.bat            run, then wait for a key press
rem   UPDATE_DATA.bat /nopause   run without waiting (scheduled tasks, CI)
rem Exit codes: 0 ok, 1 failed, 2 another refresh or write is already running.
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"

call scripts\setup_env.bat
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" scripts\update_data.py
if errorlevel 1 goto :failed
if /i not "%~1"=="/nopause" pause
exit /b 0

:failed
set "CODE=%ERRORLEVEL%"
echo.
echo UPDATE_DATA failed ^(exit code %CODE%^). See the messages above.
if /i not "%~1"=="/nopause" pause
exit /b %CODE%
