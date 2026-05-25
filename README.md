# Remote tmux Terminal GUI for Windows

![UI](UI.jpg)

## What it does

Remote tmux Terminal GUI is a lightweight Windows GUI for controlling persistent Linux terminals through SSH and `tmux`.

The main idea is simple:

- Windows GUI connects to a Linux server through Windows `ssh.exe` local port forwarding.
- The Linux server owns persistent terminals through `tmux`.
- Closing the Windows app only detaches from remote terminals.
- Remote commands keep running after the Windows app is closed.
- Reopening the Windows app can reattach to existing remote terminals.
- `Close Remote` is the only action that kills remote tmux terminals.
- WebSocket streaming is used for terminal output.
- Instant snapshot resync is triggered after input to reduce display mismatch.
- Login info can be saved on Windows, including host, port, username, key path, password via DPAPI, remote service port, default terminal directory, font size, and SSH window visibility option.
- Commands can be entered in the input box at the bottom, or typed directly in the black terminal area.
- Windows can set a default remote directory. When a new terminal is created, Linux starts the normal login-like shell first, then automatically sends one `cd -- <path>` command to the tmux pane.
- The Windows client supports adjustable terminal font size.
- The Windows client can be packaged as a standalone `.exe`.
- Multiple remote terminals can be selected and closed at once.
- `ssh.exe` console window can optionally be hidden when using SSH key or ssh-agent login.

## Architecture

```text
Windows GUI
    |
    |  Windows ssh.exe local port forwarding
    v
Linux FastAPI server
    |
    |  tmux new-session / send-keys / capture-pane
    v
Persistent Linux tmux terminals
````

The Windows app is only a client.
The real terminal state is owned by Linux `tmux`.

That means:

```text
Close Windows app     -> detach only
Reopen Windows app    -> reattach to existing tmux terminals
Click Close Remote    -> kill selected remote tmux terminals
```

## Features

### Persistent remote terminals

Remote terminals are backed by `tmux`.

If you run a long command such as:

```bash
python train.py
```

and then close the Windows GUI, the command continues running on Linux.

When you reopen the Windows GUI, you can attach to the existing remote terminal and continue viewing the output.

### WebSocket streaming

Terminal output is streamed from Linux to Windows through WebSocket.

The client also requests an instant snapshot after input operations, which helps avoid temporary display mismatch after typing commands, pressing Enter, Backspace, or control keys.

### Interactive keys

The Windows terminal area supports common interactive keys:

```text
Enter
Backspace
Tab
Arrow keys
Ctrl-C
Ctrl-D
Ctrl-Z
Esc
Ctrl-V
```

Typical use cases include:

```bash
python train.py
tail -f log.txt
nvidia-smi
conda activate myenv
```

This is still a lightweight terminal renderer, not a full xterm emulator.
Full-screen TUI programs such as `vim`, `htop`, and `less` may not behave perfectly.

### Saved login info

The Windows client can save login information:

```text
SSH host
SSH port
Username
Private key path
Remote service port
Default terminal directory
Font size
Hide ssh.exe console option
Password, encrypted by Windows DPAPI
```

Saved config location:

```text
%APPDATA%\RemoteTmuxTerminal\client_config.json
```

Password is encrypted using Windows DPAPI and is only intended for the current Windows user.

### Default remote directory

You can set a default directory for newly created terminals.

The behavior is intentionally designed to be close to normal SSH usage:

```bash
# Linux starts a normal login-like shell first.
# Then the server automatically sends:
cd -- /your/default/path
```

So environment initialization still happens before changing directory.

### Adjustable font size

The Windows GUI supports terminal font size adjustment:

```text
A-      decrease font size
A+      increase font size
number  directly set font size
```

Shortcuts:

```text
Ctrl + Plus
Ctrl + Minus
```

Font size is saved automatically and restored the next time the app opens.

### Close multiple terminals at once

The terminal list on the left supports multi-selection.

You can select one or more terminals, then click:

```text
Close Remote
```

The app will show a confirmation dialog before closing them.

Selection behavior:

```text
Ctrl + Click    select or unselect one terminal
Shift + Click   select a range of terminals
```

If no terminal is selected in the left list, `Close Remote` falls back to closing the current active terminal, preserving the old behavior.

### Optional hidden ssh.exe window

By default, the Windows app uses system `ssh.exe` to create local port forwarding.

In the connection dialog, you can enable:

```text
Hide ssh.exe console window
```

This prevents the extra `ssh.exe` console window from appearing.

Recommended usage:

```text
SSH key / ssh-agent login  -> enable Hide ssh.exe console window
Password login             -> do not enable it
```

If password login is used and the SSH window is hidden, `ssh.exe` has no place to ask for the password, so the connection may fail.

## Linux server

### Install dependencies

On Linux:

```bash
sudo apt update
sudo apt install tmux python3-venv python3-pip
```

### Start the server

Upload the `server/` directory to Linux, then run:

```bash
cd ~/remote_tmux_terminal_v19_multi_close_hidden_ssh/server
bash start_server.sh
```

By default, the server listens on:

```text
127.0.0.1:8765
```

It is intended to be accessed through SSH local port forwarding, not exposed directly to the public network.

### Start for long-running use

For long-running use, start the server inside a separate tmux session:

```bash
tmux new -s rterm_server
cd ~/remote_tmux_terminal_v19_multi_close_hidden_ssh/server
bash start_server.sh
```

Then detach:

```text
Ctrl-b, then d
```

### Health check

On the Linux server:

```bash
curl http://127.0.0.1:8765/health
```

Expected result:

```json
{"ok": true}
```

The exact returned fields may vary by version.

## Windows client

### Option 1: Run from source

If you run the source version:

```bat
windows_client\run_client.bat
```

The Windows source version uses Python standard library only.
It does not need `pip`, `paramiko`, `cryptography`, or other third-party Python packages on Windows.

However, running from source still requires Python to be installed on Windows.

### Option 2: Run packaged EXE

After packaging, run:

```text
windows_client\dist\RemoteTmuxTerminal.exe
```

The packaged EXE can be copied to another Windows machine and run directly.

The target Windows machine does not need Python.

It still needs Windows OpenSSH client:

```bat
ssh -V
```

If this command prints OpenSSH version information, `ssh.exe` is available.

## Build Windows EXE

On a Windows machine with Python or Anaconda installed, run:

```bat
windows_client\build_windows_exe_conda.bat
```

or:

```bat
windows_client\build_windows_exe.bat
```

The output will be generated at:

```text
windows_client\dist\RemoteTmuxTerminal.exe
```

The build machine needs Python.
The target machine running the generated EXE does not need Python.

## Connection settings

Typical connection fields:

```text
SSH Host
SSH Port
Username
Private key path
Remote service port
Default terminal directory
Hide ssh.exe console window
Save password
```

Example:

```text
SSH Host: 192.168.1.100
SSH Port: 22
Username: lzy
Private key path: C:\Users\lzy\.ssh\id_rsa
Remote service port: 8765
Default terminal directory: /new_data/lzy/my_project
```

## Default directory behavior

You can set the default directory in two places:

1. In the connection dialog:

```text
Default terminal directory (optional)
```

2. After the GUI opens:

```text
Set Default Dir
```

Example:

```text
/data/miku/my_project
```

When a new terminal is created, the server conceptually does this:

```bash
# normal SSH-like shell startup first
# then automatically:
cd -- /data/miku/my_project
```

Leaving the field empty means new terminals use the normal login directory.

Existing tmux terminals are not moved automatically.
The setting only affects newly created terminals.

## Conda and environment variables

New terminals are designed to behave close to normal SSH login shells.

The server-side Python virtual environment is not leaked into user terminals.

You can use Conda normally if your shell startup files initialize Conda correctly:

```bash
conda activate myenv
which python
python -c "import sys; print(sys.executable)"
```

If `conda activate` works in a normal SSH session, it should also work in newly created terminals here.

## Common workflow

1. Start Linux server:

```bash
tmux new -s rterm_server
cd ~/remote_tmux_terminal_v19_multi_close_hidden_ssh/server
bash start_server.sh
# Ctrl-b, then d
```

2. Start Windows GUI:

```bat
windows_client\run_client.bat
```

or run the packaged EXE:

```text
RemoteTmuxTerminal.exe
```

3. Connect to the Linux server.

4. Create a new terminal.

5. Run commands:

```bash
conda activate myenv
cd /data/miku/my_project
python train.py
```

6. Close the Windows app if needed.

The remote command continues running.

7. Reopen the Windows app and reattach to the terminal.

## Notes and limitations

This project is designed for persistent remote command execution and log viewing.

Good use cases:

```text
training scripts
server scripts
log viewing
nvidia-smi
tail -f
basic shell operations
Conda environment activation
```

Known limitations:

```text
Not a full xterm emulator
vim / htop / less may not work perfectly
Password login cannot be fully hidden because ssh.exe needs a password prompt
The Linux server should be accessed through SSH forwarding, not exposed publicly
```


