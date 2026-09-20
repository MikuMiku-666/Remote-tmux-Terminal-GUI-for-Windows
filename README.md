# v31 authoritative cursor preview

This directory is a self-contained isolated preview. The complete directory can
be copied to another machine without the old `server/` or `windows_client/`
directories. It does not modify the existing packaged executable.

## What changes

- tmux physical rows are preserved instead of joining wrapped rows with `-J`;
- every snapshot carries a directly mappable row and Unicode-aware character index;
- the Windows cursor is corrected by every authoritative server snapshot;
- local key prediction remains only as low-latency feedback between snapshots;
- the tmux pane is resized after the Windows terminal widget or font changes;
- all existing SSH, tabs, copy/paste, saved-login, close-remote, and command-mirror
  behavior is inherited from the current implementation.

## Linux server

From the repository root:

```bash
bash authoritative_cursor_v31/server/start_server.sh
```

On first launch, the script automatically creates an isolated `.venv` inside
the v31 server directory and installs all server dependencies. Later launches
reuse it. Dependencies are installed again only when a requirements file has
changed, so the server environment does not modify the system Python or a
training Conda environment.

Use the same SSH forwarding and port as the existing version. Stop the existing
server first if it already occupies that port, or pass a different port:

```bash
bash authoritative_cursor_v31/server/start_server.sh --port 8766
```

## Windows client

From the repository root, run:

```bat
authoritative_cursor_v31\windows_client\run_client.bat
```

The source client continues to use only the Python standard library. `wcwidth`
is installed on the Linux server, where terminal display columns are converted
to Tk character indexes.

To build a separate executable without replacing the current one:

```bat
authoritative_cursor_v31\windows_client\build_windows_exe.bat
```

The result is
`authoritative_cursor_v31\windows_client\dist\RemoteTmuxTerminal-v31.exe`.

## Checks

```bash
python -m unittest -v authoritative_cursor_v31/tests/test_protocol.py
```

Recommended manual cases: ASCII editing, Chinese text, combining accents,
left/right/home/end, command history, long wrapped commands, empty prompts,
font-size changes, and window resizing.
