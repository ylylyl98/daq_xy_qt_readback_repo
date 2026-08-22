@echo off
setlocal enabledelayedexpansion

REM --------- DAQ XY UI one-click launcher (Windows) ----------
REM Usage (optional):
REM   Start_DAQ_XY_UI.bat Dev1 ao0 ao1
REM Defaults: Dev1 ao0 ao1

cd /d "%~dp0"

set DEV=%1
set AOX=%2
set AOY=%3
if "%DEV%"=="" set DEV=Dev1
if "%AOX%"=="" set AOX=ao0
if "%AOY%"=="" set AOY=ao1

REM Pick a python launcher: prefer "py -3" (Windows Python Launcher), else "python"
set PY=py -3
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
  set PY=python
)

REM Create venv if needed
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment in .venv ...
  %PY% -m venv .venv
)

REM Activate venv
call ".venv\Scripts\activate.bat"

echo [2/3] Installing/upgrading dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m pip install -e .

echo [3/3] Launching DAQ XY UI (dev=%DEV% ao-x=%AOX% ao-y=%AOY%) ...
".venv\Scripts\python.exe" -m daq_xy_qt_readback --dev "%DEV%" --ao-x "%AOX%" --ao-y "%AOY%"
set UI_EXIT_CODE=%ERRORLEVEL%

if not "%UI_EXIT_CODE%"=="0" (
  echo.
  echo DAQ XY UI exited with error code %UI_EXIT_CODE%. Press any key to close.
  pause >nul
)

exit /b %UI_EXIT_CODE%
