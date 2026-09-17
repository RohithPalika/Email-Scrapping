@echo off
echo Starting Email Scraping Application...
call "%~dp0venv\Scripts\activate.bat"
python "%~dp0app.py"
pause
