@echo off
rem INTERNAL - called by START_TERMINAL.bat and UPDATE_DATA.bat.
rem Creates or repairs .venv and installs the locked dependencies. Exit code 0 = ready, otherwise a
rem message has already been printed. Safe to run repeatedly.
setlocal EnableExtensions
cd /d "%~dp0\.."
set "VENV_PY=.venv\Scripts\python.exe"

if not exist "%VENV_PY%" goto :create
"%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 14) else 1)" >nul 2>&1
if not errorlevel 1 goto :bootstrap
echo The existing .venv is broken or is not Python 3.14. Recreating it...
rmdir /s /q ".venv"
if exist ".venv" goto :cannot_delete

:create
set "PY_CMD="
py -3.14 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3.14"
if defined PY_CMD goto :make_venv
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 14) else 1)" >nul 2>&1
if not errorlevel 1 set "PY_CMD=python"
if not defined PY_CMD goto :no_python
:make_venv
echo Creating virtual environment with Python 3.14...
%PY_CMD% -m venv .venv
if errorlevel 1 goto :venv_failed

:bootstrap
"%VENV_PY%" scripts\bootstrap_env.py
if errorlevel 1 goto :bootstrap_failed
if not exist ".env" copy ".env.example" ".env" >nul
exit /b 0

:no_python
echo ERROR: Python 3.14 was not found. Install it from https://www.python.org/downloads/ ^(include the py launcher^).
exit /b 1
:venv_failed
echo ERROR: could not create .venv.
exit /b 1
:cannot_delete
echo ERROR: could not delete .venv. Close programs that use it and run this script again.
exit /b 1
:bootstrap_failed
echo ERROR: environment setup failed ^(see messages above^). Run this script again after fixing the problem.
echo If it keeps failing, delete the .venv folder and start over.
exit /b 1
