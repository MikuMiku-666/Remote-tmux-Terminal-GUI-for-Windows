#!/usr/bin/env python3
"""
Linux remote terminal server.

Design:
- tmux owns terminal lifetime, so client disconnects do NOT stop shells/commands.
- This service only creates/lists/kills tmux sessions and proxies input/output.
- Bind to 127.0.0.1 and reach it from Windows via SSH local port forwarding.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_NAME = "remote-tmux-terminal"
SESSION_PREFIX = "rterm_"
SESSION_ID_RE = re.compile(r"^[0-9a-f]{12}$")
STATE_DIR = Path.home() / ".remote_tmux_terminal"
META_FILE = STATE_DIR / "sessions.json"

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CreateTerminalRequest(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    cols: int = Field(default=120, ge=40, le=300)
    rows: int = Field(default=32, ge=10, le=100)
    cwd: str | None = None
    initial_cd: str | None = Field(default=None, max_length=4096)


class RenameTerminalRequest(BaseModel):
    title: str = Field(max_length=80)


class TerminalInputRequest(BaseModel):
    data: str = ""


class TerminalKeyRequest(BaseModel):
    key: str


class TerminalResizeRequest(BaseModel):
    cols: int = Field(default=120, ge=40, le=300)
    rows: int = Field(default=32, ge=10, le=100)


def ensure_tmux() -> None:
    if shutil.which("tmux") is None:
        raise HTTPException(
            status_code=500,
            detail="tmux is not installed. Install it first, e.g. sudo apt install tmux",
        )


def run_cmd(args: List[str], *, timeout: float = 5.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        cp = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"Command timed out: {' '.join(args)}") from exc

    if check and cp.returncode != 0:
        raise HTTPException(status_code=500, detail=cp.stderr.strip() or cp.stdout.strip())
    return cp


def load_meta() -> Dict[str, Dict[str, Any]]:
    try:
        if META_FILE.exists():
            return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_meta(meta: Dict[str, Dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = META_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(META_FILE)




def clean_user_path() -> str:
    """Return PATH for user terminals without the server's Python venv.

    The FastAPI server itself runs in server/.venv, but shells created for the
    user must behave like a direct SSH login shell. If the tmux pane inherits
    server/.venv/bin at the front of PATH, `python` will point to the server
    virtualenv instead of the user's conda/system Python.
    """
    raw = os.environ.get("PATH", "")
    venv = os.environ.get("VIRTUAL_ENV", "")
    venv_real = os.path.realpath(venv) if venv else ""
    keep: List[str] = []
    for part in raw.split(os.pathsep):
        if not part:
            continue
        real = os.path.realpath(part)
        if venv_real and (real == venv_real or real.startswith(venv_real + os.sep)):
            continue
        # Defensive: remove the packaged service venv even if VIRTUAL_ENV was lost.
        if "/server/.venv/" in real or real.endswith("/server/.venv/bin"):
            continue
        if real.endswith("/.venv/bin") and "/remote_tmux_terminal" in real:
            continue
        keep.append(part)
    if not keep:
        return "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return os.pathsep.join(keep)


def build_user_shell_command() -> str:
    """Build the command used as the tmux pane's shell.

    Goal: the pane should feel like `ssh user@host`:
    - do not inherit the FastAPI server's .venv;
    - load the user's login/profile files and interactive shell rc;
    - make `conda activate xxx` work if conda was initialized normally.
    """
    import shlex

    override = os.environ.get("RTERM_USER_SHELL_COMMAND")
    if override:
        return override

    clean_path = clean_user_path()
    user_shell = os.environ.get("SHELL") or "/bin/bash"
    shell_base = os.path.basename(user_shell)

    unset_line = (
        "unset VIRTUAL_ENV PYTHONHOME PYTHONPATH; "
        "unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL; "
        f"export PATH={shlex.quote(clean_path)}; "
    )

    if shell_base == "zsh":
        # zsh login shell reads ~/.zprofile and interactive zsh reads ~/.zshrc.
        inner = unset_line + f"exec {shlex.quote(user_shell)} -l"
        return f"/bin/sh -lc {shlex.quote(inner)}"

    # Bash path: source login files, then start an interactive bash so ~/.bashrc
    # and conda shell hooks are available. This intentionally favors the user's
    # normal SSH startup files over the service process environment.
    inner = (
        unset_line
        + "if [ -f ~/.bash_profile ]; then . ~/.bash_profile; "
        + "elif [ -f ~/.bash_login ]; then . ~/.bash_login; "
        + "elif [ -f ~/.profile ]; then . ~/.profile; fi; "
        + "exec /bin/bash -i"
    )
    return f"/bin/bash -lc {shlex.quote(inner)}"

def validate_id(term_id: str) -> str:
    if not SESSION_ID_RE.match(term_id):
        raise HTTPException(status_code=400, detail="Invalid terminal id")
    return term_id


def session_name(term_id: str) -> str:
    validate_id(term_id)
    return f"{SESSION_PREFIX}{term_id}"


def tmux_session_exists(name: str) -> bool:
    cp = run_cmd(["tmux", "has-session", "-t", name], check=False)
    return cp.returncode == 0


def list_tmux_sessions() -> List[Dict[str, Any]]:
    ensure_tmux()
    cp = run_cmd(
        ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_created}\t#{session_attached}\t#{session_windows}"],
        check=False,
    )
    if cp.returncode != 0:
        # tmux exits non-zero when no server/session exists.
        return []

    meta = load_meta()
    items: List[Dict[str, Any]] = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, created, attached, windows = parts[:4]
        if not name.startswith(SESSION_PREFIX):
            continue
        term_id = name[len(SESSION_PREFIX) :]
        if not SESSION_ID_RE.match(term_id):
            continue
        info = meta.get(term_id, {})
        items.append(
            {
                "id": term_id,
                "name": name,
                "title": info.get("title") or f"Terminal {term_id[:4]}",
                "created_at": info.get("created_at") or int(created),
                "tmux_created_at": int(created) if created.isdigit() else None,
                "attached": int(attached) if attached.isdigit() else 0,
                "windows": int(windows) if windows.isdigit() else 1,
            }
        )
    items.sort(key=lambda x: x.get("created_at") or 0)
    return items


def capture_pane(term_id: str, scrollback: int = 5000) -> str:
    name = session_name(term_id)
    if not tmux_session_exists(name):
        raise HTTPException(status_code=404, detail="Terminal does not exist")
    cp = run_cmd(
        ["tmux", "capture-pane", "-p", "-J", "-S", f"-{scrollback}", "-t", name],
        timeout=3.0,
    )
    return cp.stdout.rstrip("\n")


def get_cursor_info(term_id: str) -> Dict[str, int] | None:
    """Return tmux pane cursor metadata for lightweight client highlighting.

    pane_cursor_x/y are 0-based coordinates inside the visible pane, not the
    full scrollback captured by capture-pane. The Windows client maps them onto
    the last pane_height lines of the snapshot and underlines the character at
    that approximate position. This is intentionally a small cursor hint, not a
    full xterm cursor implementation.
    """
    name = session_name(term_id)
    if not tmux_session_exists(name):
        raise HTTPException(status_code=404, detail="Terminal does not exist")
    cp = run_cmd(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            name,
            "#{pane_cursor_x}\t#{pane_cursor_y}\t#{pane_width}\t#{pane_height}",
        ],
        timeout=2.0,
        check=False,
    )
    if cp.returncode != 0:
        return None
    parts = cp.stdout.strip().split("\t")
    if len(parts) < 4:
        return None
    try:
        x, y, width, height = [int(v) for v in parts[:4]]
        return {"x": x, "y": y, "width": width, "height": height}
    except Exception:
        return None


def capture_screen_state(term_id: str, scrollback: int = 5000) -> Dict[str, Any]:
    """Capture the authoritative pane text plus cursor metadata."""
    return {"data": capture_pane(term_id, scrollback=scrollback), "cursor": get_cursor_info(term_id)}


def send_text(term_id: str, data: str) -> None:
    name = session_name(term_id)
    if not tmux_session_exists(name):
        raise HTTPException(status_code=404, detail="Terminal does not exist")

    # tmux send-keys -l is literal, but newlines are better represented as Enter.
    # Supports pasted multi-line commands while avoiding shell escaping.
    data = data.replace("\r\n", "\n").replace("\r", "\n")
    parts = data.split("\n")
    for i, part in enumerate(parts):
        if part:
            run_cmd(["tmux", "send-keys", "-t", name, "-l", part], timeout=3.0)
        if i < len(parts) - 1:
            run_cmd(["tmux", "send-keys", "-t", name, "Enter"], timeout=3.0)


def queue_initial_cd(term_id: str, target_dir: str | None) -> None:
    """Queue an automatic `cd` after the login shell starts.

    The shell first starts in the normal login directory and loads the user's
    profile/rc files, then receives one ordinary cd command. This matches the
    requested behavior: normal SSH-like startup first, automatic cd second.
    """
    if not target_dir:
        return
    import shlex

    name = session_name(term_id)
    path = os.path.expanduser(str(target_dir).strip())
    if not path:
        return
    command = "cd -- " + shlex.quote(path)
    time.sleep(0.15)
    run_cmd(["tmux", "send-keys", "-t", name, "-l", command], timeout=3.0)
    run_cmd(["tmux", "send-keys", "-t", name, "Enter"], timeout=3.0)


def send_tmux_key(term_id: str, key: str) -> None:
    name = session_name(term_id)
    if not tmux_session_exists(name):
        raise HTTPException(status_code=404, detail="Terminal does not exist")
    # Normalize common GUI names to tmux key names. tmux uses BSpace; sending
    # literal "Backspace" may be echoed by some shells instead of deleting text.
    aliases = {"Backspace": "BSpace", "Return": "Enter", "Esc": "Escape"}
    key = aliases.get(key, key)
    # Small allow-list for safety and predictable UI behavior.
    allowed = {
        "Enter",
        "Escape",
        "Backspace",
        "Delete",
        "Tab",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "C-c",
        "C-d",
        "C-z",
        "C-v",
        "C-l",
        "C-a",
        "C-e",
        "C-u",
        "C-k",
        "C-w",
        "C-r",
        "C-s",
        "C-q",
        "C-p",
        "C-n",
        "C-b",
        "C-f",
        "Space",
        "BSpace",
    }
    if key not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported key: {key}")
    run_cmd(["tmux", "send-keys", "-t", name, key], timeout=3.0)


def resize_terminal(term_id: str, cols: int, rows: int) -> None:
    name = session_name(term_id)
    if not tmux_session_exists(name):
        raise HTTPException(status_code=404, detail="Terminal does not exist")
    cols = max(40, min(300, int(cols)))
    rows = max(10, min(100, int(rows)))
    run_cmd(["tmux", "resize-window", "-t", name, "-x", str(cols), "-y", str(rows)], timeout=3.0)


ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)


def decode_tmux_control_payload(payload: str) -> str:
    """Decode tmux control-mode %output payload.

    tmux escapes control bytes as octal sequences such as \012, and also
    escapes backslashes. The result is best-effort UTF-8 text suitable for the
    lightweight Tk text viewer used by the Windows client.
    """
    out = bytearray()
    i = 0
    while i < len(payload):
        ch = payload[i]
        if ch == "\\" and i + 3 < len(payload) and all(c in "01234567" for c in payload[i + 1 : i + 4]):
            out.append(int(payload[i + 1 : i + 4], 8))
            i += 4
            continue
        if ch == "\\" and i + 1 < len(payload):
            # Common tmux escaping: \\ -> \, \s -> space in some outputs.
            nxt = payload[i + 1]
            if nxt == "s":
                out.append(ord(" "))
            else:
                out.extend(nxt.encode("utf-8", errors="replace"))
            i += 2
            continue
        out.extend(ch.encode("utf-8", errors="replace"))
        i += 1
    return out.decode("utf-8", errors="replace")


def normalize_stream_text(text: str) -> str:
    """Remove ANSI escape sequences but preserve CR/LF for the client renderer.

    v7 converted carriage returns to newlines on the server. That made the
    lightweight Windows viewer briefly show an extra blank line after Enter
    until the periodic snapshot corrected it. v8 keeps CR and LF intact; the
    client handles CRLF as a newline and bare CR as a same-line redraw.
    """
    return ANSI_ESCAPE_RE.sub("", text)


@app.get("/health")
def health() -> Dict[str, Any]:
    ensure_tmux()
    return {"ok": True, "app": APP_NAME, "version": "v26-local-cursor", "tmux": True, "time": time.time()}


@app.get("/terminals")
def list_terminals() -> Dict[str, Any]:
    return {"terminals": list_tmux_sessions()}


@app.post("/terminals")
def create_terminal(req: CreateTerminalRequest) -> Dict[str, Any]:
    ensure_tmux()
    term_id = uuid.uuid4().hex[:12]
    name = session_name(term_id)
    title = (req.title or f"Terminal {term_id[:4]}").strip() or f"Terminal {term_id[:4]}"

    # Start a user login-like interactive shell, not the server's Python venv.
    command = build_user_shell_command()
    tmux_cmd = [
        "tmux",
        "new-session",
        "-d",
        "-s",
        name,
        "-x",
        str(req.cols),
        "-y",
        str(req.rows),
    ]
    # Make new terminals start like a normal SSH login: in the user's home
    # directory by default, not in the FastAPI server directory.  Starting in
    # server/ can break user startup files that source relative environment
    # scripts such as `source setup.bash`, and it also feels different from
    # direct SSH.  RTERM_DEFAULT_CWD can override this globally.
    cwd_candidate = req.cwd or os.environ.get("RTERM_DEFAULT_CWD") or str(Path.home())
    cwd = os.path.expanduser(cwd_candidate)
    if os.path.isdir(cwd):
        tmux_cmd += ["-c", cwd]
    tmux_cmd += [command]
    run_cmd(tmux_cmd, timeout=5.0)
    run_cmd(["tmux", "rename-window", "-t", name, title[:40]], timeout=3.0, check=False)
    queue_initial_cd(term_id, req.initial_cd)

    meta = load_meta()
    meta[term_id] = {"title": title, "created_at": int(time.time()), "initial_cd": req.initial_cd or ""}
    save_meta(meta)
    return {"terminal": next(x for x in list_tmux_sessions() if x["id"] == term_id)}


@app.patch("/terminals/{term_id}")
def rename_terminal(term_id: str, req: RenameTerminalRequest) -> Dict[str, Any]:
    validate_id(term_id)
    name = session_name(term_id)
    if not tmux_session_exists(name):
        raise HTTPException(status_code=404, detail="Terminal does not exist")
    meta = load_meta()
    meta.setdefault(term_id, {})["title"] = req.title.strip() or f"Terminal {term_id[:4]}"
    save_meta(meta)
    run_cmd(["tmux", "rename-window", "-t", name, meta[term_id]["title"][:40]], timeout=3.0, check=False)
    return {"ok": True}


@app.delete("/terminals/{term_id}")
def kill_terminal(term_id: str) -> Dict[str, Any]:
    validate_id(term_id)
    name = session_name(term_id)
    if tmux_session_exists(name):
        run_cmd(["tmux", "kill-session", "-t", name], timeout=5.0)
    meta = load_meta()
    meta.pop(term_id, None)
    save_meta(meta)
    return {"ok": True}


@app.get("/terminals/{term_id}/snapshot")
def snapshot(term_id: str) -> Dict[str, Any]:
    validate_id(term_id)
    state = capture_screen_state(term_id)
    return {"id": term_id, **state}


@app.post("/terminals/{term_id}/input")
def terminal_input(term_id: str, req: TerminalInputRequest) -> Dict[str, Any]:
    validate_id(term_id)
    send_text(term_id, req.data)
    return {"ok": True}


@app.post("/terminals/{term_id}/key")
def terminal_key(term_id: str, req: TerminalKeyRequest) -> Dict[str, Any]:
    validate_id(term_id)
    send_tmux_key(term_id, req.key)
    return {"ok": True}


@app.post("/terminals/{term_id}/resize")
def terminal_resize(term_id: str, req: TerminalResizeRequest) -> Dict[str, Any]:
    validate_id(term_id)
    resize_terminal(term_id, req.cols, req.rows)
    return {"ok": True}


@app.websocket("/ws/{term_id}")
async def websocket_terminal(ws: WebSocket, term_id: str) -> None:
    """Efficient terminal attachment over one persistent WebSocket.

    v4/v6 used repeated HTTP snapshot polling from Windows. v7 keeps the HTTP
    endpoints for create/list/delete, but an attached tab receives terminal
    updates over a single WebSocket. The server uses tmux control mode for
    low-latency incremental output, plus a low-frequency snapshot resync to
    correct lightweight text rendering when commands use cursor control.
    """
    validate_id(term_id)
    await ws.accept()

    name = session_name(term_id)
    if not tmux_session_exists(name):
        await ws.send_text(json.dumps({"type": "error", "message": "Terminal does not exist"}))
        await ws.close(code=1008)
        return

    stop = asyncio.Event()
    output_queue: "asyncio.Queue[str]" = asyncio.Queue()
    # v23: same-line redraws such as tqdm/progress bars can update the tmux
    # pane without producing a visually correct append-only stream in the
    # lightweight Windows renderer.  A dirty-screen watcher checks the actual
    # tmux pane content at a short interval and pushes a snapshot immediately
    # when the rendered content changes.  This replaces the old user-visible
    # 1.5s correction delay with near-real-time server-side push.
    dirty_event = asyncio.Event()
    proc: asyncio.subprocess.Process | None = None
    send_lock = asyncio.Lock()
    # The Windows client reports whether this tab is the active visible tab.
    # Active tabs get fast progress/tqdm updates; inactive tabs back off to a
    # slower cadence to reduce long-running server overhead.
    client_active = True

    async def safe_send(payload: Dict[str, Any]) -> None:
        # Several tasks can send concurrently: tmux stream flushing, periodic
        # snapshots, receiver-triggered instant snapshots, and errors. Serialize
        # writes so WebSocket frames do not interleave.
        async with send_lock:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_snapshot_now() -> None:
        state = await asyncio.to_thread(capture_screen_state, term_id)
        await safe_send({"type": "snapshot", "id": term_id, **state})

    async def control_reader() -> None:
        """Read incremental pane output from `tmux -C attach-session`."""
        nonlocal proc
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "-C",
                "attach-session",
                "-t",
                name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            # Fallback to periodic snapshots if control mode is unavailable.
            await output_queue.put(f"\n[server] tmux control mode failed, falling back to snapshot resync: {exc}\n")
            while not stop.is_set():
                await asyncio.sleep(1.0)
            return

        assert proc.stdout is not None
        while not stop.is_set():
            try:
                raw = await proc.stdout.readline()
            except Exception as exc:
                await output_queue.put(f"\n[server] tmux control reader failed: {exc}\n")
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("%output "):
                parts = line.split(" ", 2)
                if len(parts) >= 3:
                    decoded = normalize_stream_text(decode_tmux_control_payload(parts[2]))
                    if decoded:
                        await output_queue.put(decoded)
                        dirty_event.set()
            elif line.startswith("%exit"):
                break
            # Other control-mode records such as %begin/%end/%layout-change are
            # not useful for this lightweight viewer.

    async def output_flusher() -> None:
        """Coalesce small output chunks to avoid one WebSocket frame per byte."""
        while not stop.is_set():
            try:
                first = await asyncio.wait_for(output_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            chunks = [first]
            deadline = time.monotonic() + 0.05
            while time.monotonic() < deadline:
                try:
                    chunks.append(output_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            data = "".join(chunks)
            if data:
                await safe_send({"type": "output", "id": term_id, "data": data})

    async def screen_change_pusher() -> None:
        """Push snapshots adaptively when the tmux screen changes.

        v23 used a fixed short interval so tqdm/progress bars were corrected
        quickly. v24 keeps the same responsiveness while reducing long-running
        overhead:
          - after recent output/change: high-frequency checks (default 0.10s);
          - after a few idle seconds: slow checks (default 0.75s);
          - when the Windows tab/window is inactive: slower checks (default 1.00s);
          - when the WebSocket disconnects: this task is cancelled/stopped.

        Environment tuning:
          RTERM_SCREEN_PUSH_INTERVAL=0.10          # legacy alias for fast
          RTERM_SCREEN_PUSH_FAST_INTERVAL=0.10
          RTERM_SCREEN_PUSH_IDLE_INTERVAL=0.75
          RTERM_SCREEN_PUSH_INACTIVE_INTERVAL=1.00
          RTERM_SCREEN_PUSH_IDLE_AFTER=2.50
        """
        def read_float(name: str, default: float, lo: float, hi: float) -> float:
            try:
                value = float(os.environ.get(name, str(default)))
            except Exception:
                value = default
            return max(lo, min(hi, value))

        fast_default = read_float("RTERM_SCREEN_PUSH_INTERVAL", 0.10, 0.03, 2.0)
        fast_interval = read_float("RTERM_SCREEN_PUSH_FAST_INTERVAL", fast_default, 0.03, 2.0)
        idle_interval = read_float("RTERM_SCREEN_PUSH_IDLE_INTERVAL", 0.75, fast_interval, 5.0)
        inactive_interval = read_float("RTERM_SCREEN_PUSH_INACTIVE_INTERVAL", 1.00, fast_interval, 10.0)
        idle_after = read_float("RTERM_SCREEN_PUSH_IDLE_AFTER", 2.50, 0.2, 30.0)

        last_state: Dict[str, Any] | None = None
        last_change_time = time.monotonic()

        while not stop.is_set():
            try:
                now = time.monotonic()
                if not client_active:
                    interval = inactive_interval
                elif now - last_change_time <= idle_after:
                    interval = fast_interval
                else:
                    interval = idle_interval

                # Wake immediately when tmux control mode reports output, but
                # still scan on timeout to catch redraw-only changes.
                try:
                    await asyncio.wait_for(dirty_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                dirty_event.clear()

                state = await asyncio.to_thread(capture_screen_state, term_id)
                # Compare both data and cursor. This lets simple cursor movement
                # after input update the underline hint even if the text is same.
                comparable = {"data": state.get("data", ""), "cursor": state.get("cursor")}
                if comparable != last_state:
                    last_state = comparable
                    last_change_time = time.monotonic()
                    await safe_send({"type": "snapshot", "id": term_id, **state})
            except Exception as exc:
                await safe_send({"type": "error", "message": str(exc)})
                stop.set()
                break

            # Hard rate limit based on the current state. This is what keeps a
            # busy progress bar smooth while letting idle/inactive tabs back off.
            now = time.monotonic()
            if not client_active:
                sleep_for = inactive_interval
            elif now - last_change_time <= idle_after:
                sleep_for = fast_interval
            else:
                sleep_for = idle_interval
            await asyncio.sleep(sleep_for)

    async def receiver() -> None:
        nonlocal client_active
        while not stop.is_set():
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                stop.set()
                break
            try:
                msg = json.loads(raw)
                typ = msg.get("type")
                if typ == "input":
                    await asyncio.to_thread(send_text, term_id, str(msg.get("data", "")))
                    # v11: immediately resync after user input/key echo. The
                    # lightweight Windows Text view does not fully emulate ANSI
                    # cursor movement, so raw incremental echo can appear at a
                    # wrong visual column until the periodic 1.5s snapshot.
                    await send_snapshot_now()
                elif typ == "key":
                    await asyncio.to_thread(send_tmux_key, term_id, str(msg.get("key", "")))
                    await send_snapshot_now()
                elif typ == "resize":
                    await asyncio.to_thread(resize_terminal, term_id, int(msg.get("cols", 120)), int(msg.get("rows", 32)))
                    await send_snapshot_now()
                elif typ == "close_remote":
                    await asyncio.to_thread(kill_terminal, term_id)
                    stop.set()
                    break
                elif typ == "active":
                    client_active = bool(msg.get("active", True))
                    dirty_event.set()
                elif typ == "ping":
                    await safe_send({"type": "pong", "time": time.time()})
            except Exception as exc:
                await safe_send({"type": "error", "message": str(exc)})

    # Immediate initial state before incremental output arrives.
    try:
        await send_snapshot_now()
    except Exception as exc:
        await safe_send({"type": "error", "message": str(exc)})
        await ws.close(code=1008)
        return

    tasks = [
        asyncio.create_task(control_reader()),
        asyncio.create_task(output_flusher()),
        asyncio.create_task(screen_change_pusher()),
        asyncio.create_task(receiver()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    for task in pending:
        task.cancel()
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Remote tmux terminal server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Keep 127.0.0.1 when using SSH tunnel.")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()

    ensure_tmux()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
