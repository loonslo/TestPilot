@echo off
title TestPilot
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 goto :existing
echo Preparing TestPilot...
call uv sync --locked --python 3.14
if errorlevel 1 goto :failed
goto :run
:existing
if not exist ".venv\Scripts\python.exe" (
    echo Please install uv, then double-click start.bat again.
    goto :failed
)
:run
".venv\Scripts\python.exe" -c "import sys, api_test_agent; assert sys.version_info >= (3,14)"
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m api_test_agent.cli ui %*
if errorlevel 1 goto :failed
exit /b 0
:failed
echo TestPilot could not start. See the error above.
pause
exit /b 1
