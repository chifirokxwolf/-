@echo off
setlocal
cd /d "%~dp0.."
python -m pip install "playwright>=1.49,<2"
if errorlevel 1 goto failed
echo Setup completed. Run tools\check_claude_account.cmd next.
pause
exit /b 0
:failed
echo Setup failed. Use the Python installation that runs Cardinal.
pause
exit /b 1
