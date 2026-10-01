@echo off
rem Starts the US Stock Research Terminal (local only, http://127.0.0.1:8501).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment with Python 3.14...
    py -3.14 -m venv .venv
    if errorlevel 1 (
        echo Could not create the virtual environment. Install Python 3.14 from python.org ^(with the py launcher^).
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed. Delete the .venv folder and try again.
        pause
        exit /b 1
    )
)

if not exist ".env" copy ".env.example" ".env" >nul

".venv\Scripts\python.exe" scripts\init_db.py
if errorlevel 1 (
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m streamlit run app\main.py
if errorlevel 1 pause
