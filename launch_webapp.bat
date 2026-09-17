@echo off
title Email Scraper Web App
cd /d "%~dp0"
echo ===================================================
echo       Starting Email Scraper Web Application...
echo ===================================================
echo Server starting at http://127.0.0.1:5000
echo Opening browser automatically...
echo.
call "%~dp0venv\Scripts\activate.bat"
python "%~dp0app.py"
pause
