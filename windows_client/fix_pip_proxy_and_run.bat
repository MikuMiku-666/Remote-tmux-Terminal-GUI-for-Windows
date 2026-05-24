@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo [INFO] Cleaning proxy-related environment variables and pip config...
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "ALL_PROXY="
set "all_proxy="
set "PIP_PROXY="
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m pip config unset global.proxy >nul 2>nul
    .venv\Scripts\python.exe -m pip config unset install.proxy >nul 2>nul
) else (
    py -3 -m pip config unset global.proxy >nul 2>nul
    py -3 -m pip config unset install.proxy >nul 2>nul
)

call run_client.bat
