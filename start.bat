@echo off
title PULSUS AI - Email Scraper
cd /d "%~dp0"
echo Starting PULSUS AI Email Scraping Application...
if exist "%~dp0venv\Scripts\activate.bat" (
    call "%~dp0venv\Scripts\activate.bat"
)
python "%~dp0run.py"
pause
