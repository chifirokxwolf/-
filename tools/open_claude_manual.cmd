@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
if not exist "tools\claude_manual_login.py" goto missing
python tools\claude_manual_login.py
set "CLAUDE_TOOL_EXIT=%errorlevel%"
pause
exit /b %CLAUDE_TOOL_EXIT%
:missing
echo ERROR: Extract the entire ZIP first, then copy the tools folder into Cardinal.
echo Do not run this CMD from inside the ZIP archive.
pause
exit /b 1
