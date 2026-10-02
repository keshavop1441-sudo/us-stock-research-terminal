@echo off
rem Starts the US Stock Research Terminal on http://127.0.0.1:8501 (this computer only).
rem   START_TERMINAL.bat              set up if needed, start, open the browser
rem   START_TERMINAL.bat /setup-only  only prepare .venv and the database
rem   START_TERMINAL.bat /smoke       start, check it is healthy, stop (used by CI)
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"

call scripts\setup_env.bat
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" scripts\init_db.py
if errorlevel 1 goto :failed

if /i "%~1"=="/setup-only" exit /b 0
if /i "%~1"=="/smoke" (".venv\Scripts\python.exe" scripts\launch_terminal.py --smoke --port 8599 --no-browser) else (".venv\Scripts\python.exe" scripts\launch_terminal.py)
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo START_TERMINAL failed. See the messages above.
if /i not "%~1"=="/smoke" if /i not "%~1"=="/setup-only" pause
exit /b 1
