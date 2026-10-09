@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Prefer the project-local virtualenv if it exists; fall back to whatever
REM python is on PATH. This keeps the script portable across machines.
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)
echo [Hermes mode] Starting WeChat AI Bot...
echo [Hermes mode] python: %PYEXE%
"%PYEXE%" wechat_ai_bot_dev.py %*
pause
