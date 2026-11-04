@echo off
title TestPilot - Launcher
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto :check_args

echo [INFO] Environment not detected, initializing...
where uv >nul 2>nul
if %ERRORLEVEL% equ 0 (
    call uv sync --locked --python 3.14
)
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
    goto :check_args
)

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PYTHON_EXE=python"
    goto :check_args
)

echo [ERROR] Python 3.14 or uv not found. Please install Python 3.14 or uv.
pause
exit /b 1

:check_args
if not "%~1"=="" goto :run_ui_direct

:show_menu
cls
echo ========================================================================
echo                   TestPilot Launcher
echo ========================================================================
echo.
echo   [1] Start Web Console (Auto-opens browser)
echo   [2] Run Synthetic Demo (Offline end-to-end verification)
echo   [3] Import OpenAPI / Swagger from URL
echo   [0] Exit
echo.
echo ========================================================================
echo.

choice /C 1230 /T 3 /D 1 /M "Select option [Default: 1 in 3s]"
if %ERRORLEVEL% equ 1 goto :run_ui
if %ERRORLEVEL% equ 2 goto :run_demo
if %ERRORLEVEL% equ 3 goto :run_import
if %ERRORLEVEL% equ 4 exit /b 0

:run_ui
echo.
echo [1/2] Starting TestPilot Web Console...
echo [2/2] Opening browser at http://127.0.0.1:8080/
echo.
echo ========================================================================
echo   Web Console is now running!
echo   Open in browser: http://127.0.0.1:8080/
echo   To stop the server, simply close this window or press Ctrl+C.
echo ========================================================================
echo.
"%PYTHON_EXE%" -m api_test_agent.cli ui
goto :finish

:run_ui_direct
"%PYTHON_EXE%" -m api_test_agent.cli ui %*
goto :finish

:run_demo
echo.
echo [1/4] Creating demo task quickstart_demo...
"%PYTHON_EXE%" -m api_test_agent.cli task create quickstart_demo --project SyntheticDemo --commit 0000000000000000000000000000000000000000 --scope "*" >nul 2>&1

echo [2/4] Configuring document openapi-demo.json...
"%PYTHON_EXE%" -m api_test_agent.cli task configure quickstart_demo --document-paths openapi-demo.json >nul 2>&1

echo [3/4] Discovering operations...
"%PYTHON_EXE%" -m api_test_agent.cli task discover quickstart_demo --materials-root examples/tasks

echo.
echo [4/4] Extracted Inventory Summary:
echo ------------------------------------------------------------------------
"%PYTHON_EXE%" -m api_test_agent.cli task materials quickstart_demo
echo ------------------------------------------------------------------------
echo.
echo ========================================================================
echo   Demo completed successfully!
echo   Assets saved to: runtime/tasks/quickstart_demo/
echo ========================================================================
echo.
pause
goto :finish

:run_import
echo.
echo Please enter the OpenAPI/Swagger URL (e.g. http://127.0.0.1:8080/v2/api-docs):
set /p DOC_URL="URL: "
if "%DOC_URL%"=="" goto :show_menu

set /p TASK_NAME="Task Name [Default: my_service]: "
if "%TASK_NAME%"=="" set "TASK_NAME=my_service"

echo.
echo Fetching and parsing OpenAPI from: %DOC_URL% ...
"%PYTHON_EXE%" -m api_test_agent.cli task import-openapi %TASK_NAME% --project %TASK_NAME% --commit "0000000000000000000000000000000000000000" --url "%DOC_URL%"

if %ERRORLEVEL% equ 0 (
    echo.
    echo ========================================================================
    echo   Import succeeded! Now launching Web Console to view inventory...
    echo ========================================================================
    echo.
    timeout /t 2 >nul
    "%PYTHON_EXE%" -m api_test_agent.cli ui
) else (
    echo.
    echo [ERROR] Import failed. Please check if the URL is accessible.
    pause
)
goto :finish

:finish
if %ERRORLEVEL% neq 0 (
    echo.
    echo Exited. Press any key to close...
    pause >nul
)
