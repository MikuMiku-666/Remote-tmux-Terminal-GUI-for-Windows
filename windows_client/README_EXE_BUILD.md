# Build RemoteTmuxTerminal.exe

## Goal

Generate a standalone Windows GUI executable:

```text
dist\RemoteTmuxTerminal.exe
```

The target Windows machine does not need Python or pip.

## Build with normal Python

Double-click:

```text
build_windows_exe.bat
```

## Build with Anaconda

Open Anaconda Prompt, enter this folder, then run:

```bat
build_windows_exe_conda.bat
```

## Runtime requirements on the target machine

The EXE still needs Windows OpenSSH Client:

```bat
ssh -V
```

If missing, install it from Windows Optional Features.

## Password login

Password login opens a small SSH console because Windows OpenSSH owns the password prompt. The GUI copies the saved password to your clipboard; paste it into that console.

For the best packaged-app experience, use SSH key login.

## Crash log

If the GUI crashes, check:

```text
%APPDATA%\RemoteTmuxTerminal\client_error.log
```
