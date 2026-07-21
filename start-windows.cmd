@echo off
setlocal
cd /d "%~dp0"

if not exist .env (
  copy /y .env.example .env >nul
  notepad .env
  echo Created .env. Fill it in, then double-click start-windows.cmd again.
  pause
  exit /b 0
)

where py >nul 2>nul
if %errorlevel%==0 (
  set "PYTHON_CMD=py -3"
) else (
  set "PYTHON_CMD=python"
)

%PYTHON_CMD% dm_agent.py doctor
if errorlevel 1 (
  echo Runtime check failed. Fix the items above and try again.
  pause
  exit /b 1
)

%PYTHON_CMD% dm_agent.py run --open-dashboard
if errorlevel 1 pause
