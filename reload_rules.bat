@echo off
REM ============================================================
REM   Reload custom strategy scripts (stock-selector)
REM
REM   Edit any file under user_rules\ then double-click this.
REM   It validates every script (syntax + performance gate) and,
REM   if the server is running, makes it pick up the new code
REM   without a restart.
REM
REM   Exit code 1 means at least one script failed to load or
REM   did not pass the performance gate.
REM ============================================================
setlocal
cd /d "%~dp0"

REM Force UTF-8 codepage so the Chinese report is not garbled
chcp 65001 >nul

REM Prefer Windows native tools, avoid Git-Bash/MSYS hijacking PATH
set "PATH=%SystemRoot%\System32;%PATH%"

python reload_rules.py %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [WARN] Some scripts were rejected. See the report above.
)

pause
exit /b %RC%
