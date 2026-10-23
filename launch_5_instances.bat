@echo off
title PULSUS AI Multi-Terminal Parallel Launcher
cd /d "%~dp0"

echo ===================================================================
echo   ⚡ PULSUS AI - Launching 5 Parallel Extraction Terminals
echo   Shared Central Master Database with Zero Duplicate Guarantee
echo ===================================================================
echo.

set "PY_CMD=python"
if exist "%~dp0venv\Scripts\python.exe" set "PY_CMD=%~dp0venv\Scripts\python.exe"

echo [1/5] Launching Terminal on Port 5000...
start "Pulsus AI [Port 5000]" cmd /k ""%PY_CMD%" "%~dp0run.py" --port 5000"
timeout /t 2 /nobreak >nul

echo [2/5] Launching Terminal on Port 5001...
start "Pulsus AI [Port 5001]" cmd /k ""%PY_CMD%" "%~dp0run.py" --port 5001"
timeout /t 2 /nobreak >nul

echo [3/5] Launching Terminal on Port 5002...
start "Pulsus AI [Port 5002]" cmd /k ""%PY_CMD%" "%~dp0run.py" --port 5002"
timeout /t 2 /nobreak >nul

echo [4/5] Launching Terminal on Port 5003...
start "Pulsus AI [Port 5003]" cmd /k ""%PY_CMD%" "%~dp0run.py" --port 5003"
timeout /t 2 /nobreak >nul

echo [5/5] Launching Terminal on Port 5004...
start "Pulsus AI [Port 5004]" cmd /k ""%PY_CMD%" "%~dp0run.py" --port 5004"

echo.
echo ===================================================================
echo   All 5 instances launched successfully!
echo   - http://127.0.0.1:5000
echo   - http://127.0.0.1:5001
echo   - http://127.0.0.1:5002
echo   - http://127.0.0.1:5003
echo   - http://127.0.0.1:5004
echo.
echo   Each terminal operates independently across all 10 databases.
echo   All instances share the single master deduplication database!
echo ===================================================================
pause
