@echo off
cd /d "%~dp0.."
python --version
if errorlevel 1 goto failed
python -m pip install "playwright>=1.49,<2"
if errorlevel 1 goto failed
python -m playwright install chromium
if errorlevel 1 goto failed
echo Browser setup completed. Run tools\open_claude_session.cmd next.
pause
exit /b 0
:failed
echo Setup failed. Use the same Python installation that runs Cardinal.
pause
exit /b 1
