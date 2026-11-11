@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
if not exist "tools\claude_account_check.py" goto missing
if not exist "tools\claude_browser_ui.py" goto missing
if not exist "tools\claude_browser_actions.py" goto missing
if not exist "tools\claude_manual_login.py" goto missing
python tools\claude_account_check.py
pause
exit /b
:missing
echo ERROR: Extract the entire ZIP and copy its tools folder into Cardinal.
pause
exit /b 1
