@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM 让系统命令优先走 Windows 原生版本，避免被 Git-Bash/MSYS 的 PATH 劫持
set "PATH=%SystemRoot%\System32;%PATH%"

echo ============================================
echo   实时选股系统 (stock-selector) 一键启动
echo ============================================
echo.

REM ---- 第1步：清理占用 8000 端口的旧进程 ----
echo [1/3] 检查端口 8000 占用情况...
set "KILLED=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000 "') do (
    echo       发现占用进程 PID %%a，正在结束...
    taskkill /F /PID %%a >nul 2>&1
    set "KILLED=1"
)
if "!KILLED!"=="1" (
    echo       旧进程已清理。
) else (
    echo       端口 8000 空闲，无需清理。
)
echo.

REM ---- 第2步：启动服务器（独立最小化窗口，便于查看日志） ----
echo [2/3] 启动选股系统服务器...
start "stock-selector-server" /min python main.py
if errorlevel 1 (
    echo       [错误] 服务器进程启动失败，请确认 python 与依赖已安装。
    pause
    exit /b 1
)
echo       服务器进程已启动（最小化窗口 stock-selector-server）。
echo.

REM ---- 第3步：轮询等待服务就绪，然后打开浏览器 ----
echo [3/3] 等待服务器就绪...
set /a TRIES=0
:wait_loop
ping -n 2 127.0.0.1 >nul
set /a TRIES+=1
curl -s -o nul -m 2 http://127.0.0.1:8000/api/status
if !errorlevel! equ 0 goto :server_ready
if !TRIES! geq 30 goto :server_timeout
goto :wait_loop

:server_ready
echo       服务器已就绪，正在打开浏览器...
start "" http://127.0.0.1:8000
echo.
echo ============================================
echo   启动成功：http://127.0.0.1:8000
echo   战法验证页：http://127.0.0.1:8000/backtest
echo ============================================
ping -n 3 127.0.0.1 >nul
exit /b 0

:server_timeout
echo       [警告] 等待 30 秒服务器仍未响应。
echo       请查看最小化的服务器窗口 (stock-selector-server) 中的报错信息。
pause
exit /b 1
