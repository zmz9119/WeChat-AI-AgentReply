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
echo [DSH mode] config: wechat_config_dsh.json
echo [DSH mode] python: %PYEXE%
"%PYEXE%" run_wechat_dsh.py %*
pause
