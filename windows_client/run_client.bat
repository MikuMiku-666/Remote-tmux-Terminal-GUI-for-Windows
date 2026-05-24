@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM Windows launcher with ZERO third-party Python dependencies. v8 uses stdlib WebSocket streaming with smoother Enter rendering.
REM It does not create .venv and does not run pip, avoiding pip/tomllib/proxy/
REM cryptography/Rust DLL problems on Windows.
REM ---------------------------------------------------------------------------
set "PYTHONUTF8=1"
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python launcher py.exe was not found.
    echo Please install Python 3.9+ from https://www.python.org/ and check "Add python.exe to PATH".
    pause
    exit /b 1
)

where ssh >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ssh.exe was not found.
    echo Please install Windows OpenSSH Client.
    echo PowerShell as administrator:
    echo Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
    pause
    exit /b 1
)

echo [INFO] Starting Windows client without pip dependencies...
py -3 client.py
if errorlevel 1 goto error
exit /b 0

:error
echo.
echo [ERROR] Windows client failed to start.
echo Copy the full text in this window and send it back.
pause
exit /b 1
