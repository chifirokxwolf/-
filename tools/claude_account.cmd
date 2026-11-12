@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
python tools\claude_account_menu.py
set "CLAUDE_TOOL_EXIT=%errorlevel%"
if not "%CLAUDE_TOOL_EXIT%"=="0" pause
exit /b %CLAUDE_TOOL_EXIT%
