@echo off
title PULSUS AI - Email Scraper Web App
cd /d "%~dp0"
echo ===================================================
echo   ⚡ PULSUS AI - Starting Web Application...
echo ===================================================
echo Server starting at http://127.0.0.1:5000
echo Opening browser automatically...
echo.
if exist "%~dp0venv\Scripts\activate.bat" (
    call "%~dp0venv\Scripts\activate.bat"
)
python "%~dp0run.py"
pause
