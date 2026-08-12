@echo off
REM ============================================================
REM   Backtest history data downloader (stock-selector)
REM   Double-click = download default scope (stock pool, 30m+daily).
REM   Or pass args, e.g.:
REM     download.bat --scope watch --period daily
REM     download.bat --codes 600519,000858 --start 2022-01-01
REM     download.bat --dry-run
REM   Data source is BaoStock (globally serialized, ~2.5s per task),
REM   so a full pool download takes several minutes. This is expected.
REM ============================================================
setlocal
cd /d "%~dp0"

REM Force UTF-8 codepage so the Chinese progress output is not garbled
REM (download.py reconfigures stdout to UTF-8).
chcp 65001 >nul

REM Prefer the Windows native tools, avoid Git-Bash/MSYS hijacking PATH
set "PATH=%SystemRoot%\System32;%PATH%"

python download.py %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [WARN] Downloader exited with code %RC%. See messages above.
)

REM Keep the window open so the summary and failure list stay readable
pause
exit /b %RC%
