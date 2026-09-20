@echo off
setlocal
pushd "%~dp0"
python -m PyInstaller --noconfirm --clean RemoteTmuxTerminal-v31.spec
set ERR=%ERRORLEVEL%
popd
exit /b %ERR%
