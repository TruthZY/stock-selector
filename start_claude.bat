@echo off
cd /d "%~dp0"

REM Strip trailing backslash from %~dp0
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM Check if Windows Terminal is available
where wt >nul 2>nul
if %errorlevel%==0 (
    start "" wt -d "%SCRIPT_DIR%" cmd /k claude
    exit
)

REM Fallback to cmd
echo Windows Terminal not found, starting Claude in cmd...
start "" cmd /k claude
