#!/usr/bin/env python3
"""
Windows GUI client for the remote tmux terminal server.

No third-party Python dependencies on Windows source mode. v20 fixes black terminal copy/paste and keeps PyInstaller packaging and multi-terminal close.
- Uses Windows built-in ssh.exe for SSH local port forwarding.
- Uses a small stdlib-only WebSocket client for terminal streaming.
- Keeps urllib only for create/list/delete/login health calls.
- Closing this client only detaches. It never kills remote tmux terminals
  unless you click "Close Remote".
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import queue
import socket
import hashlib
import struct
import threading
import subprocess
import shutil
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk


CONFIG_DIR = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / "RemoteTmuxTerminal"
CONFIG_FILE = CONFIG_DIR / "client_config.json"
APP_VERSION = "v20: Based on v19, fixes terminal copy/paste; no v12 cursor."
ERROR_LOG_FILE = CONFIG_DIR / "client_error.log"


def log_error(text: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.write(text + "\n")
    except Exception:
        pass


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_encrypt(raw: bytes) -> str:
    """Encrypt bytes with Windows DPAPI and return base64 text.

    DPAPI binds the encrypted value to the current Windows user account.
    No third-party dependency is needed.
    """
    if os.name != "nt":
        raise RuntimeError("Password saving is only supported on Windows in this client.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buf = ctypes.create_string_buffer(raw)
    in_blob = DATA_BLOB(len(raw), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "RemoteTmuxTerminal password",
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


def _dpapi_decrypt(encoded: str) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Password loading is only supported on Windows in this client.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    encrypted = base64.b64decode(encoded.encode("ascii"))
    in_buf = ctypes.create_string_buffer(encrypted)
    in_blob = DATA_BLOB(len(encrypted), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


def load_client_config() -> Dict[str, Any]:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_client_config(data: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def clear_saved_profile() -> None:
    data = load_client_config()
    data.pop("last_profile", None)
    data.pop("default_cwd", None)
    save_client_config(data)


def get_saved_default_cwd() -> str:
    data = load_client_config()
    profile = data.get("last_profile", {}) if isinstance(data.get("last_profile", {}), dict) else {}
    return str(profile.get("default_cwd") or data.get("default_cwd") or "")


def save_default_cwd(default_cwd: str) -> None:
    data = load_client_config()
    data["default_cwd"] = default_cwd
    profile = data.get("last_profile")
    if isinstance(profile, dict):
        profile["default_cwd"] = default_cwd
    save_client_config(data)


def get_saved_font_size() -> int:
    data = load_client_config()
    try:
        size = int(data.get("font_size", 10))
    except Exception:
        size = 10
    return max(6, min(32, size))


def save_font_size(size: int) -> None:
    data = load_client_config()
    data["font_size"] = max(6, min(32, int(size)))
    save_client_config(data)


def decrypt_saved_password(profile: Dict[str, Any]) -> str:
    payload = profile.get("password_dpapi")
    if not payload:
        return ""
    try:
        return _dpapi_decrypt(str(payload)).decode("utf-8")
    except Exception:
        return ""


def save_connection_profile(cfg: "SSHConfig", *, remember_login: bool, save_password: bool) -> None:
    if not remember_login:
        clear_saved_profile()
        return
    profile: Dict[str, Any] = {
        "host": cfg.host,
        "port": cfg.port,
        "username": cfg.username,
        "key_filename": cfg.key_filename,
        "remote_port": cfg.remote_port,
        "default_cwd": cfg.default_cwd,
        "hide_ssh_window": bool(cfg.hide_ssh_window),
        "save_password": bool(save_password),
        "updated_at": int(time.time()),
    }
    if save_password and cfg.password:
        profile["password_dpapi"] = _dpapi_encrypt(cfg.password.encode("utf-8"))
    data = load_client_config()
    data["last_profile"] = profile
    data["default_cwd"] = cfg.default_cwd
    save_client_config(data)


@dataclass
class SSHConfig:
    host: str
    port: int
    username: str
    password: str = ""
    key_filename: str = ""
    remote_host: str = "127.0.0.1"
    remote_port: int = 8765
    default_cwd: str = ""
    hide_ssh_window: bool = False
    local_host: str = "127.0.0.1"
    local_port: int = 0


class SSHTunnel:
    """Local port forward implemented with the system OpenSSH client."""

    def __init__(self, cfg: SSHConfig) -> None:
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen[str]] = None
        self.local_port: Optional[int] = None

    @staticmethod
    def _pick_free_port(host: str = "127.0.0.1") -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    def start(self) -> int:
        ssh_exe = shutil.which("ssh") or shutil.which("ssh.exe")
        if not ssh_exe:
            raise RuntimeError(
                "OpenSSH client was not found. Install Windows OpenSSH Client, "
                "or run in PowerShell as administrator: "
                "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
            )

        local_port = self.cfg.local_port or self._pick_free_port(self.cfg.local_host)
        target = f"{self.cfg.username}@{self.cfg.host}"
        forward = f"{self.cfg.local_host}:{local_port}:{self.cfg.remote_host}:{self.cfg.remote_port}"
        cmd = [
            ssh_exe,
            "-N",
            "-L",
            forward,
            "-p",
            str(self.cfg.port),
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.cfg.key_filename:
            cmd += ["-i", self.cfg.key_filename]
        cmd.append(target)

        # v19: optional hidden ssh.exe console window.
        # Hidden mode uses BatchMode=yes + CREATE_NO_WINDOW, so it is suitable for
        # SSH key / ssh-agent login. Password-only login still needs the visible
        # ssh.exe console, otherwise there is nowhere to type the password.
        popen_kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            # Accept a new host key automatically for the common first-run case.
            # Existing changed host keys are still rejected by OpenSSH.
            cmd[1:1] = ["-o", "StrictHostKeyChecking=accept-new"]
            if self.cfg.hide_ssh_window:
                cmd[1:1] = ["-o", "BatchMode=yes"]
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                popen_kwargs["stdin"] = subprocess.DEVNULL
                popen_kwargs["stdout"] = subprocess.DEVNULL
                popen_kwargs["stderr"] = subprocess.DEVNULL
            elif self.cfg.password or not self.cfg.key_filename:
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            else:
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                popen_kwargs["stdin"] = subprocess.DEVNULL
                popen_kwargs["stdout"] = subprocess.DEVNULL
                popen_kwargs["stderr"] = subprocess.DEVNULL

        self.proc = subprocess.Popen(cmd, **popen_kwargs)
        self.local_port = local_port
        time.sleep(0.8)
        if self.proc.poll() is not None:
            raise RuntimeError(
                "ssh.exe exited immediately. Check host, port, username, key path, SSH permission, "
                "or whether the remote service is reachable through the tunnel."
            )
        return local_port

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class ConnectDialog(simpledialog.Dialog):
    def body(self, master: tk.Widget) -> tk.Widget:
        self.title("Connect to Linux server via SSH")
        saved_config = load_client_config()
        saved_profile = saved_config.get("last_profile", {}) if isinstance(saved_config.get("last_profile", {}), dict) else {}
        saved_password = decrypt_saved_password(saved_profile)

        labels = [
            ("SSH Host", "host"),
            ("SSH Port", "port"),
            ("Username", "username"),
            ("Password", "password"),
            ("Private key path (optional)", "key"),
            ("Remote service port", "remote_port"),
            ("Default terminal directory (optional)", "default_cwd"),
        ]
        self.vars: Dict[str, tk.StringVar] = {
            "host": tk.StringVar(value=str(saved_profile.get("host", ""))),
            "port": tk.StringVar(value=str(saved_profile.get("port", "22"))),
            "username": tk.StringVar(value=str(saved_profile.get("username", ""))),
            "password": tk.StringVar(value=saved_password),
            "key": tk.StringVar(value=str(saved_profile.get("key_filename", ""))),
            "remote_port": tk.StringVar(value=str(saved_profile.get("remote_port", "8765"))),
            "default_cwd": tk.StringVar(value=str(saved_profile.get("default_cwd") or saved_config.get("default_cwd") or "")),
        }
        self.remember_var = tk.BooleanVar(value=True)
        self.save_password_var = tk.BooleanVar(value=bool(saved_profile.get("save_password") and saved_password))
        self.hide_ssh_window_var = tk.BooleanVar(value=bool(saved_profile.get("hide_ssh_window", False)))
        self.remember_login = True
        self.save_password = False
        self.hide_ssh_window = False

        first: Optional[ttk.Entry] = None
        for row, (label, key) in enumerate(labels):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
            show = "*" if key == "password" else None
            entry = ttk.Entry(master, textvariable=self.vars[key], width=46, show=show)
            entry.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
            if first is None:
                first = entry

        row = len(labels)
        ttk.Checkbutton(
            master,
            text="Remember host / port / username / key path next time",
            variable=self.remember_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=(8, 2))
        row += 1
        ttk.Checkbutton(
            master,
            text="Save password locally with Windows DPAPI encryption",
            variable=self.save_password_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        row += 1
        ttk.Checkbutton(
            master,
            text="Hide ssh.exe console window (recommended with SSH key / ssh-agent)",
            variable=self.hide_ssh_window_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        row += 1
        ttk.Label(
            master,
            text="Note: Hidden mode removes the ssh.exe popup, but password-only SSH cannot be typed while hidden. Use SSH key / ssh-agent, or uncheck this option to show the password console.",
            wraplength=430,
            foreground="#666666",
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=(6, 2))

        master.columnconfigure(1, weight=1)
        return first or master

    def validate(self) -> bool:
        try:
            host = self.vars["host"].get().strip()
            username = self.vars["username"].get().strip()
            port = int(self.vars["port"].get().strip())
            remote_port = int(self.vars["remote_port"].get().strip())
            if not host or not username:
                raise ValueError("SSH Host and Username are required")
            self.remember_login = bool(self.remember_var.get())
            self.save_password = bool(self.save_password_var.get())
            self.hide_ssh_window = bool(self.hide_ssh_window_var.get())
            self.result = SSHConfig(
                host=host,
                port=port,
                username=username,
                password=self.vars["password"].get(),
                key_filename=self.vars["key"].get().strip(),
                remote_port=remote_port,
                default_cwd=self.vars["default_cwd"].get().strip(),
                hide_ssh_window=self.hide_ssh_window,
            )
            return True
        except Exception as exc:
            messagebox.showerror("Invalid configuration", str(exc))
            return False


class ApiClient:
    def __init__(self, local_port: int) -> None:
        self.base = f"http://127.0.0.1:{local_port}"

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {msg}") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc


class SimpleWebSocket:
    """Tiny RFC 6455 client for ws://127.0.0.1 over the SSH tunnel.

    This avoids websocket-client/paramiko/cryptography on Windows while still
    giving us a persistent low-latency stream instead of HTTP polling.
    It intentionally supports only the subset we need: text frames, ping/pong,
    close frames, no TLS, no extensions.
    """

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url: str, timeout: float = 8.0) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws":
            raise ValueError("Only ws:// URLs are supported by this stdlib client")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(None)
        self._send_lock = threading.Lock()
        self._recv_buffer = b""
        self._closed = False
        self._handshake()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(headers.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake failed: connection closed")
            response += chunk
            if len(response) > 65536:
                raise RuntimeError("WebSocket handshake response too large")
        header_blob, _, rest = response.partition(b"\r\n\r\n")
        self._recv_buffer = rest
        header_text = header_blob.decode("iso-8859-1", errors="replace")
        if " 101 " not in header_text.split("\r\n", 1)[0]:
            raise RuntimeError("WebSocket handshake failed:\n" + header_text)
        accept_expected = base64.b64encode(hashlib.sha1((key + self.GUID).encode("ascii")).digest()).decode("ascii")
        if accept_expected.lower() not in header_text.lower():
            raise RuntimeError("WebSocket handshake failed: bad Sec-WebSocket-Accept")

    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        remaining = n
        if self._recv_buffer:
            take = self._recv_buffer[:remaining]
            chunks.append(take)
            self._recv_buffer = self._recv_buffer[len(take):]
            remaining -= len(take)
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise RuntimeError("WebSocket connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self._closed:
            raise RuntimeError("WebSocket is closed")
        fin_opcode = 0x80 | (opcode & 0x0F)
        length = len(payload)
        if length < 126:
            header = bytes([fin_opcode, 0x80 | length])
        elif length < (1 << 16):
            header = bytes([fin_opcode, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([fin_opcode, 0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x1:  # text
                return payload.decode("utf-8", errors="replace")
            if opcode == 0x8:  # close
                self._closed = True
                raise RuntimeError("WebSocket closed by server")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x0:  # continuation, rare for our payload sizes
                return payload.decode("utf-8", errors="replace")
            # Ignore binary/unknown frames.

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class TerminalTab:
    """One attached tmux terminal tab using WebSocket streaming.

    v7 replaces the old HTTP polling loop with one persistent WebSocket per
    attached tab. Inputs and control keys go through the same socket, and output
    arrives as pushed incremental stream messages. A low-frequency full snapshot
    from the server keeps the lightweight text view corrected after reconnects
    or cursor-control output.

    v10 note: snapshot refresh no longer calls Text.see("end"), because that
    makes Tk horizontally scroll to the end of long prompt lines. Instead we
    scroll vertically to the bottom while keeping the left edge visible.
    """

    SPECIAL_KEYS = {
        "Return": "Enter",
        "KP_Enter": "Enter",
        "Escape": "Escape",
        "BackSpace": "BSpace",
        "Delete": "Delete",
        "Tab": "Tab",
        "Up": "Up",
        "Down": "Down",
        "Left": "Left",
        "Right": "Right",
        "Home": "Home",
        "End": "End",
        "Prior": "PageUp",
        "Next": "PageDown",
        "Page_Up": "PageUp",
        "Page_Down": "PageDown",
    }

    CONTROL_KEYS = {
        "a": "C-a",
        "b": "C-b",
        "c": "C-c",
        "d": "C-d",
        "e": "C-e",
        "f": "C-f",
        "k": "C-k",
        "l": "C-l",
        "n": "C-n",
        "p": "C-p",
        "q": "C-q",
        "r": "C-r",
        "s": "C-s",
        "u": "C-u",
        "w": "C-w",
        "z": "C-z",
    }

    CTRL_MASK = 0x0004
    SHIFT_MASK = 0x0001
    MAX_TEXT_LINES = 12000

    def __init__(self, app: "RemoteTerminalApp", info: Dict[str, Any]) -> None:
        self.app = app
        self.info = info
        self.term_id = info["id"]
        self.title = info.get("title") or self.term_id
        self.frame = ttk.Frame(app.notebook)
        self.stop_event = threading.Event()
        self.ws_thread: Optional[threading.Thread] = None
        self.ws: Optional[SimpleWebSocket] = None
        self.ws_lock = threading.Lock()
        self.connected_stream = False
        # v13: tmux control-mode incremental output can contain cursor-motion
        # sequences around shell prompts. This lightweight Tk viewer does not
        # fully emulate xterm, so immediately after local input we briefly
        # suppress raw stream echo and rely on an instant server snapshot. That
        # prevents freshly typed text from appearing separated from the prompt
        # until the periodic resync catches up.
        self.suppress_output_until = 0.0

        text_frame = ttk.Frame(self.frame)
        text_frame.pack(side="top", fill="both", expand=True)
        self.text = tk.Text(
            text_frame,
            wrap="none",
            state="disabled",
            font=("Consolas", self.app.font_size),
            bg="#101010",
            fg="#eeeeee",
            insertbackground="#ffffff",
        )
        self.v_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        self.h_scroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self.v_scroll.set, xscrollcommand=self.h_scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.v_scroll.grid(row=0, column=1, sticky="ns")
        self.h_scroll.grid(row=1, column=0, sticky="ew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self.text.bind("<Button-1>", lambda _e: self.text.focus_set())
        self.text.bind("<Button-3>", self.show_terminal_context_menu)
        self.text.bind("<KeyPress>", self.on_terminal_key)
        # v20: explicit copy/paste bindings. The generic <KeyPress> handler
        # deliberately intercepts Ctrl-C for remote SIGINT, so copy needs
        # specific bindings and context-menu actions. Ctrl-C copies when a
        # local selection exists; otherwise it still sends remote Ctrl-C.
        self.text.bind("<Control-c>", self.on_terminal_ctrl_c)
        self.text.bind("<Control-C>", self.on_terminal_ctrl_c)
        self.text.bind("<Control-Shift-c>", self.on_terminal_copy)
        self.text.bind("<Control-Shift-C>", self.on_terminal_copy)
        self.text.bind("<Control-Insert>", self.on_terminal_copy)
        self.text.bind("<Control-v>", self.on_terminal_paste)
        self.text.bind("<Control-V>", self.on_terminal_paste)
        self.text.bind("<Shift-Insert>", self.on_terminal_paste)
        self.text.bind("<<Copy>>", self.on_terminal_copy)
        self.text.bind("<<Paste>>", self.on_terminal_paste)

        self.context_menu = tk.Menu(self.text, tearoff=0)
        self.context_menu.add_command(label="Copy selected text", command=self.copy_selected_output)
        self.context_menu.add_command(label="Copy all terminal output", command=self.copy_all_output)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Paste clipboard to remote", command=self.paste_clipboard_to_remote)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Select all", command=self.select_all_output)

        bottom = ttk.Frame(self.frame)
        bottom.pack(side="bottom", fill="x")
        self.cmd_var = tk.StringVar()
        self.entry = ttk.Entry(bottom, textvariable=self.cmd_var)
        self.entry.pack(side="left", fill="x", expand=True, padx=4, pady=4)
        self.entry.bind("<Return>", lambda _e: self.send_command())
        self.entry.bind("<Control-c>", lambda _e: self._send_key_break("C-c"))
        self.entry.bind("<Control-d>", lambda _e: self._send_key_break("C-d"))
        self.entry.bind("<Control-z>", lambda _e: self._send_key_break("C-z"))
        ttk.Button(bottom, text="Send", command=self.send_command).pack(side="left", padx=2)
        ttk.Button(bottom, text="Ctrl-C", command=lambda: self.send_key("C-c")).pack(side="left", padx=2)
        ttk.Button(bottom, text="Ctrl-D", command=lambda: self.send_key("C-d")).pack(side="left", padx=2)
        ttk.Button(bottom, text="Ctrl-Z", command=lambda: self.send_key("C-z")).pack(side="left", padx=2)
        ttk.Button(bottom, text="Tab", command=lambda: self.send_key("Tab")).pack(side="left", padx=2)
        ttk.Button(bottom, text="Esc", command=lambda: self.send_key("Escape")).pack(side="left", padx=2)
        ttk.Button(bottom, text="Clear", command=lambda: self.send_key("C-l")).pack(side="left", padx=2)
        ttk.Button(bottom, text="Copy Sel", command=self.copy_selected_output).pack(side="left", padx=2)
        ttk.Button(bottom, text="Paste", command=self.paste_clipboard_to_remote).pack(side="left", padx=2)

        hint = ttk.Label(
            self.frame,
            text=APP_VERSION + " Click black area for interactive keys. Ctrl-C copies selected text; otherwise interrupts. Ctrl+V pastes to remote.",
            anchor="w",
        )
        hint.pack(side="bottom", fill="x")

        app.notebook.add(self.frame, text=self.title[:18])
        app.notebook.select(self.frame)
        self.start_streaming()

    def set_font_size(self, size: int) -> None:
        self.text.configure(font=("Consolas", size))

    def _ws_url(self) -> str:
        if self.app.local_port is None:
            raise RuntimeError("Not connected")
        return f"ws://127.0.0.1:{self.app.local_port}/ws/{urllib.parse.quote(self.term_id)}"

    def start_streaming(self) -> None:
        def worker() -> None:
            self.app.ui_queue.put(("status", f"Attached via WebSocket: {self.title}"))
            while not self.stop_event.is_set():
                ws: Optional[SimpleWebSocket] = None
                try:
                    ws = SimpleWebSocket(self._ws_url(), timeout=8.0)
                    with self.ws_lock:
                        self.ws = ws
                        self.connected_stream = True
                    self.app.ui_queue.put(("status", f"WebSocket stream connected: {self.title}"))
                    while not self.stop_event.is_set():
                        raw = ws.recv_text()
                        msg = json.loads(raw)
                        typ = msg.get("type")
                        if typ == "snapshot":
                            self.app.ui_queue.put(("snapshot", self.term_id, msg.get("data", "")))
                        elif typ == "output":
                            self.app.ui_queue.put(("output", self.term_id, msg.get("data", "")))
                        elif typ == "error":
                            self.app.ui_queue.put(("status", f"Remote error in {self.title}: {msg.get('message', '')}"))
                        elif typ == "pong":
                            pass
                except Exception as exc:
                    if not self.stop_event.is_set():
                        self.app.ui_queue.put(("status", f"WebSocket reconnecting for {self.title}: {exc}"))
                        time.sleep(1.5)
                finally:
                    with self.ws_lock:
                        if self.ws is ws:
                            self.ws = None
                            self.connected_stream = False
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception:
                            pass
            self.app.ui_queue.put(("status", f"Detached: {self.title}"))

        self.ws_thread = threading.Thread(target=worker, name=f"ws-{self.term_id}", daemon=True)
        self.ws_thread.start()

    def _scroll_bottom_keep_left(self) -> None:
        """Follow live output vertically without cropping the left edge.

        Tk Text.see("end") tries to make the insertion cursor visible in both
        directions. For long shell prompts that moves the horizontal viewport to
        the right, so the left side of the terminal appears missing after every
        snapshot resync. A terminal view should normally stay pinned to column 0.
        """
        try:
            self.text.yview_moveto(1.0)
            self.text.xview_moveto(0.0)
        except Exception:
            pass

    def set_snapshot(self, data: str) -> None:
        # A snapshot is authoritative, so streaming echo suppression can end.
        self.suppress_output_until = 0.0
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", data)
        self.text.configure(state="disabled")
        self._scroll_bottom_keep_left()

    def append_output(self, data: str) -> None:
        if not data:
            return
        # Right after local input, skip the raw echo. The server immediately
        # sends a full capture-pane snapshot, which places the typed text at
        # the correct prompt column. This avoids the old temporary visual gap.
        if time.monotonic() < self.suppress_output_until:
            return
        # Interpret the most common terminal cursor characters instead of
        # blindly converting them to newlines. This prevents the old visual
        # glitch where pressing Enter first added an empty line and only the
        # next snapshot corrected it.
        self.text.configure(state="normal")
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\r":
                # CRLF is a real newline. A bare CR is usually a progress-line
                # redraw, so reset the current visual line instead of adding an
                # extra blank line.
                if i + 1 < len(data) and data[i + 1] == "\n":
                    self.text.insert("end", "\n")
                    i += 2
                else:
                    self.text.delete("end-1c linestart", "end-1c lineend")
                    i += 1
                continue
            if ch == "\n":
                self.text.insert("end", "\n")
                i += 1
                continue
            if ch == "\b":
                try:
                    self.text.delete("end-2c", "end-1c")
                except Exception:
                    pass
                i += 1
                continue
            self.text.insert("end", ch)
            i += 1
        try:
            line_count = int(self.text.index("end-1c").split(".")[0])
            if line_count > self.MAX_TEXT_LINES:
                self.text.delete("1.0", f"{line_count - self.MAX_TEXT_LINES}.0")
        except Exception:
            pass
        self.text.configure(state="disabled")
        self._scroll_bottom_keep_left()

    def send_command(self) -> str:
        cmd = self.cmd_var.get()
        data = "\n" if not cmd else cmd + "\n"
        self.cmd_var.set("")
        self._send_message({"type": "input", "data": data}, fallback_endpoint="input")
        return "break"

    def send_key(self, key: str) -> None:
        self._send_message({"type": "key", "key": key}, fallback_endpoint="key")

    def _send_key_break(self, key: str) -> str:
        self.send_key(key)
        return "break"

    def _has_selection(self) -> bool:
        try:
            self.text.index("sel.first")
            self.text.index("sel.last")
            return True
        except Exception:
            return False

    def _get_selected_text(self) -> str:
        try:
            return self.text.get("sel.first", "sel.last")
        except Exception:
            return ""

    def show_terminal_context_menu(self, event: tk.Event) -> str:
        self.text.focus_set()
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.context_menu.grab_release()
            except Exception:
                pass
        return "break"

    def copy_selected_output(self) -> str:
        selected = self._get_selected_text()
        if not selected:
            self.app.set_status("No terminal text selected. Drag in the black area first, or use Copy all terminal output.")
            return "break"
        try:
            self.app.clipboard_clear()
            self.app.clipboard_append(selected)
            self.app.update_idletasks()
            self.app.set_status(f"Copied {len(selected)} character(s) from terminal output.")
        except Exception as exc:
            self.app.set_status(f"Copy failed: {exc}")
        return "break"

    def copy_all_output(self) -> str:
        try:
            data = self.text.get("1.0", "end-1c")
        except Exception:
            data = ""
        if not data:
            self.app.set_status("Terminal output is empty; nothing to copy.")
            return "break"
        try:
            self.app.clipboard_clear()
            self.app.clipboard_append(data)
            self.app.update_idletasks()
            self.app.set_status(f"Copied all terminal output ({len(data)} character(s)).")
        except Exception as exc:
            self.app.set_status(f"Copy all failed: {exc}")
        return "break"

    def select_all_output(self) -> str:
        try:
            self.text.tag_add("sel", "1.0", "end-1c")
            self.text.mark_set("insert", "1.0")
            self.text.focus_set()
            self.app.set_status("Selected all terminal output.")
        except Exception as exc:
            self.app.set_status(f"Select all failed: {exc}")
        return "break"

    def paste_clipboard_to_remote(self) -> str:
        try:
            data = self.app.clipboard_get()
        except Exception:
            self.app.set_status("Clipboard is empty or does not contain text.")
            return "break"
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        if data:
            self._send_message({"type": "input", "data": data}, fallback_endpoint="input")
            self.app.set_status(f"Pasted {len(data)} character(s) to remote terminal.")
        return "break"

    def on_terminal_paste(self, event: tk.Event | None = None) -> str:
        return self.paste_clipboard_to_remote()

    def on_terminal_copy(self, event: tk.Event | None = None) -> str:
        return self.copy_selected_output()

    def on_terminal_ctrl_c(self, event: tk.Event | None = None) -> str:
        # User-friendly behavior: if local terminal text is selected, Ctrl-C
        # copies it; if nothing is selected, Ctrl-C remains remote SIGINT.
        if self._has_selection():
            return self.copy_selected_output()
        self.send_key("C-c")
        return "break"

    def _copy_selection(self) -> str:
        # Kept for backward compatibility with older handlers.
        return self.copy_selected_output()

    def on_terminal_key(self, event: tk.Event) -> str:
        if (event.state & self.CTRL_MASK) and (event.state & self.SHIFT_MASK) and event.keysym.lower() == "c":
            return self.copy_selected_output()
        if (event.state & self.CTRL_MASK) and event.keysym.lower() == "v":
            return self.paste_clipboard_to_remote()
        if (event.state & self.CTRL_MASK) and event.keysym.lower() == "c":
            return self.on_terminal_ctrl_c(event)
        if event.state & self.CTRL_MASK:
            key = self.CONTROL_KEYS.get(event.keysym.lower())
            if key:
                self.send_key(key)
                return "break"
            return "break"
        key = self.SPECIAL_KEYS.get(event.keysym)
        if key:
            self.send_key(key)
            return "break"
        ch = event.char
        if ch and ch >= " " and ch != "\x7f":
            self._send_message({"type": "input", "data": ch}, fallback_endpoint="input")
            return "break"
        return "break"

    def _send_message(self, msg: Dict[str, Any], fallback_endpoint: str) -> None:
        if msg.get("type") in {"input", "key"}:
            # Give the server-side immediate snapshot time to arrive before
            # rendering low-level terminal echo that may include cursor motion.
            self.suppress_output_until = time.monotonic() + 0.8
        text = json.dumps(msg, ensure_ascii=False)
        with self.ws_lock:
            ws = self.ws
        if ws is not None:
            try:
                ws.send_text(text)
                return
            except Exception:
                # Fall through to HTTP endpoint so key presses are not lost during reconnect.
                pass
        self._post_http_async(fallback_endpoint, {k: v for k, v in msg.items() if k != "type"})

    def _post_http_async(self, endpoint: str, body: Dict[str, Any]) -> None:
        def worker() -> None:
            try:
                self.app.api.request(
                    "POST",
                    f"/terminals/{urllib.parse.quote(self.term_id)}/{endpoint}",
                    body,
                    timeout=5.0,
                )
                # Fallback HTTP path also asks for an immediate snapshot so the
                # display is corrected quickly even while WebSocket reconnects.
                snap = self.app.api.request("GET", f"/terminals/{urllib.parse.quote(self.term_id)}/snapshot", timeout=5.0)
                self.app.ui_queue.put(("snapshot", self.term_id, snap.get("data", "")))
            except Exception:
                self.app.ui_queue.put(("error", traceback.format_exc()))

        threading.Thread(target=worker, name=f"fallback-post-{self.term_id}-{endpoint}", daemon=True).start()

    def detach(self) -> None:
        self.stop_event.set()
        with self.ws_lock:
            ws = self.ws
            self.ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


class RemoteTerminalApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Remote tmux Terminal Client")
        self.geometry("1000x680")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.tunnel: Optional[SSHTunnel] = None
        self.api: Optional[ApiClient] = None
        self.local_port: Optional[int] = None
        self.tabs: Dict[str, TerminalTab] = {}
        self.terminals: Dict[str, Dict[str, Any]] = {}
        self.terminal_order: list[str] = []
        self.default_cwd: str = get_saved_default_cwd()
        self.font_size: int = get_saved_font_size()
        self.ui_queue: "queue.Queue[Any]" = queue.Queue()

        self._build_ui()
        self.bind_all("<Control-plus>", lambda _e: self.increase_font_size())
        self.bind_all("<Control-equal>", lambda _e: self.increase_font_size())
        self.bind_all("<Control-minus>", lambda _e: self.decrease_font_size())
        self.bind_all("<Control-KP_Add>", lambda _e: self.increase_font_size())
        self.bind_all("<Control-KP_Subtract>", lambda _e: self.decrease_font_size())
        self.after(100, self.process_ui_queue)
        self.after(200, self.open_connection_dialog)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(side="top", fill="x")
        ttk.Button(toolbar, text="Connect", command=self.open_connection_dialog).pack(side="left", padx=4, pady=4)
        ttk.Button(toolbar, text="Refresh", command=self.refresh_terminals).pack(side="left", padx=4, pady=4)
        ttk.Button(toolbar, text="New Terminal", command=self.create_terminal).pack(side="left", padx=4, pady=4)
        ttk.Button(toolbar, text="Attach Selected", command=self.attach_selected).pack(side="left", padx=4, pady=4)
        ttk.Button(toolbar, text="Close Remote", command=self.close_remote_selected).pack(side="left", padx=4, pady=4)
        ttk.Button(toolbar, text="Set Default Dir", command=self.set_default_dir).pack(side="left", padx=4, pady=4)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=4, pady=4)
        ttk.Button(toolbar, text="A-", width=3, command=self.decrease_font_size).pack(side="left", padx=(4, 1), pady=4)
        self.font_size_var = tk.StringVar(value=str(self.font_size))
        self.font_size_spin = ttk.Spinbox(
            toolbar,
            from_=6,
            to=32,
            width=4,
            textvariable=self.font_size_var,
            command=self.on_font_spinbox_changed,
        )
        self.font_size_spin.pack(side="left", padx=1, pady=4)
        self.font_size_spin.bind("<Return>", lambda _e: self.on_font_spinbox_changed())
        self.font_size_spin.bind("<FocusOut>", lambda _e: self.on_font_spinbox_changed())
        ttk.Button(toolbar, text="A+", width=3, command=self.increase_font_size).pack(side="left", padx=(1, 4), pady=4)
        ttk.Button(toolbar, text="Forget Saved Login", command=self.forget_saved_login).pack(side="left", padx=4, pady=4)

        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(side="top", fill="both", expand=True)

        left = ttk.Frame(main, width=240)
        ttk.Label(left, text="Remote tmux terminals").pack(side="top", anchor="w", padx=6, pady=4)
        self.listbox = tk.Listbox(left, selectmode="extended", exportselection=False)
        self.listbox.pack(side="top", fill="both", expand=True, padx=6, pady=4)
        self.listbox.bind("<Double-Button-1>", lambda _e: self.attach_selected())
        main.add(left, weight=1)

        self.notebook = ttk.Notebook(main)
        main.add(self.notebook, weight=4)

        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(side="bottom", fill="x")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def show_error(self, text: str) -> None:
        self.set_status(text)
        messagebox.showerror("Error", text)

    def open_connection_dialog(self) -> None:
        dialog = ConnectDialog(self)
        cfg = dialog.result
        if cfg is None:
            return

        remember_login = bool(getattr(dialog, "remember_login", True))
        save_password = bool(getattr(dialog, "save_password", False))
        self.default_cwd = cfg.default_cwd
        try:
            save_connection_profile(cfg, remember_login=remember_login, save_password=save_password)
        except Exception as exc:
            messagebox.showwarning("Save login failed", f"Could not save the login profile:\n{exc}")

        if cfg.password and not cfg.hide_ssh_window:
            # ssh.exe will not accept the password through stdin; it asks in its own console.
            # Copying helps the common password-login workflow while avoiding extra dependencies.
            try:
                self.clipboard_clear()
                self.clipboard_append(cfg.password)
                self.set_status("Password copied to clipboard. Paste it into the SSH console if prompted.")
            except Exception:
                pass
        elif cfg.password and cfg.hide_ssh_window and not cfg.key_filename:
            self.set_status("Hidden SSH mode enabled. Password is saved but cannot be typed into hidden ssh.exe; use SSH key / ssh-agent or uncheck Hide.")

        def worker() -> None:
            try:
                if self.tunnel is not None:
                    self.tunnel.stop()
                tunnel = SSHTunnel(cfg)
                local_port = tunnel.start()
                api = ApiClient(local_port)
                api.request("GET", "/health")
                self.ui_queue.put(("connected", tunnel, api, local_port))
            except Exception:
                self.ui_queue.put(("error", "Connection failed:\n" + traceback.format_exc()))

        self.set_status("Connecting over SSH...")
        threading.Thread(target=worker, name="connect-worker", daemon=True).start()

    def apply_font_size(self, size: int) -> None:
        self.font_size = max(6, min(32, int(size)))
        try:
            self.font_size_var.set(str(self.font_size))
        except Exception:
            pass
        for tab in list(self.tabs.values()):
            tab.set_font_size(self.font_size)
        try:
            save_font_size(self.font_size)
        except Exception as exc:
            messagebox.showwarning("Save font size failed", f"Could not save the font size:\n{exc}")
        self.set_status(f"Terminal font size set to {self.font_size}. Shortcut: Ctrl+Plus / Ctrl+Minus.")

    def on_font_spinbox_changed(self) -> None:
        try:
            size = int(str(self.font_size_var.get()).strip())
        except Exception:
            self.font_size_var.set(str(self.font_size))
            return
        self.apply_font_size(size)

    def increase_font_size(self) -> str:
        self.apply_font_size(self.font_size + 1)
        return "break"

    def decrease_font_size(self) -> str:
        self.apply_font_size(self.font_size - 1)
        return "break"

    def set_default_dir(self) -> None:
        value = simpledialog.askstring(
            "Default terminal directory",
            "Remote Linux directory to cd into automatically when creating a new terminal.\nLeave empty to use the normal login directory.",
            initialvalue=self.default_cwd,
        )
        if value is None:
            return
        self.default_cwd = value.strip()
        try:
            save_default_cwd(self.default_cwd)
        except Exception as exc:
            messagebox.showwarning("Save default directory failed", f"Could not save the default directory:\n{exc}")
        if self.default_cwd:
            self.set_status(f"Default terminal directory set to: {self.default_cwd}")
        else:
            self.set_status("Default terminal directory cleared; new terminals use the normal login directory.")

    def forget_saved_login(self) -> None:
        if not messagebox.askyesno("Forget saved login", "Remove the saved host, username, key path, saved password, and default directory from this Windows account?"):
            return
        try:
            clear_saved_profile()
            self.set_status("Saved login removed.")
        except Exception as exc:
            self.show_error(f"Could not remove saved login:\n{exc}")

    def refresh_terminals(self) -> None:
        if self.api is None:
            self.set_status("Not connected")
            return

        def worker() -> None:
            try:
                data = self.api.request("GET", "/terminals")
                self.ui_queue.put(("terminals", data.get("terminals", [])))
            except Exception:
                self.ui_queue.put(("error", traceback.format_exc()))

        threading.Thread(target=worker, name="refresh-worker", daemon=True).start()

    def render_terminal_list(self, terminals: list[Dict[str, Any]]) -> None:
        self.terminals = {t["id"]: t for t in terminals}
        self.terminal_order = [t["id"] for t in terminals]
        self.listbox.delete(0, "end")
        for t in terminals:
            label = f"{t.get('title', t['id'])}  [{t['id']}]"
            self.listbox.insert("end", label)
        self.set_status(f"{len(terminals)} remote terminal(s). Double click to attach. Multi-select on the left, then Close Remote to kill selected terminals.")

    def create_terminal(self) -> None:
        if self.api is None:
            self.set_status("Not connected")
            return
        title = simpledialog.askstring("New terminal", "Terminal title:", initialvalue="Terminal")
        if title is None:
            return

        default_cwd = self.default_cwd.strip()

        def worker() -> None:
            try:
                body = {"title": title, "cols": 120, "rows": 32}
                if default_cwd:
                    body["initial_cd"] = default_cwd
                data = self.api.request("POST", "/terminals", body)
                term = data["terminal"]
                self.ui_queue.put(("created", term))
            except Exception:
                self.ui_queue.put(("error", traceback.format_exc()))

        threading.Thread(target=worker, name="create-worker", daemon=True).start()

    def selected_terminal_id(self) -> Optional[str]:
        selection = self.listbox.curselection()
        if not selection:
            current = self.notebook.select()
            for term_id, tab in self.tabs.items():
                if str(tab.frame) == current:
                    return term_id
            return None
        index = int(selection[0])
        ids = self.terminal_order
        if 0 <= index < len(ids):
            return ids[index]
        return None

    def selected_terminal_ids(self) -> list[str]:
        """Return all terminals selected in the left list.

        If nothing is selected in the list, fall back to the currently active tab
        so the old single-terminal Close Remote workflow still works.
        """
        selection = self.listbox.curselection()
        result: list[str] = []
        for raw_index in selection:
            index = int(raw_index)
            if 0 <= index < len(self.terminal_order):
                term_id = self.terminal_order[index]
                if term_id in self.terminals and term_id not in result:
                    result.append(term_id)
        if result:
            return result

        current = self.notebook.select()
        for term_id, tab in self.tabs.items():
            if str(tab.frame) == current:
                return [term_id]
        return []

    def attach_selected(self) -> None:
        term_id = self.selected_terminal_id()
        if term_id is None:
            self.set_status("Select a remote terminal first.")
            return
        self.attach_terminal(self.terminals[term_id])

    def attach_terminal(self, info: Dict[str, Any]) -> None:
        term_id = info["id"]
        if term_id in self.tabs:
            self.notebook.select(self.tabs[term_id].frame)
            return
        try:
            self.tabs[term_id] = TerminalTab(self, info)
        except Exception as exc:
            self.show_error(str(exc))

    def close_remote_selected(self) -> None:
        if self.api is None:
            return
        term_ids = self.selected_terminal_ids()
        if not term_ids:
            self.set_status("Select one or more remote terminals first.")
            return

        titles = [self.terminals.get(term_id, {}).get("title", term_id) for term_id in term_ids]
        if len(term_ids) == 1:
            prompt = (
                "This will kill the Linux tmux terminal and stop its running command:\n\n"
                f"{titles[0]}\n\nContinue?"
            )
        else:
            preview = "\n".join(f"- {title}" for title in titles[:12])
            if len(titles) > 12:
                preview += f"\n... and {len(titles) - 12} more"
            prompt = (
                f"This will kill {len(term_ids)} Linux tmux terminals and stop any running commands inside them:\n\n"
                f"{preview}\n\nContinue?"
            )

        if not messagebox.askyesno("Close remote terminal(s)", prompt):
            return

        def worker() -> None:
            closed: list[str] = []
            failed: list[str] = []
            for term_id in term_ids:
                try:
                    self.api.request("DELETE", f"/terminals/{urllib.parse.quote(term_id)}")
                    closed.append(term_id)
                except Exception:
                    failed.append(f"{term_id}: {traceback.format_exc()}")
            if closed:
                self.ui_queue.put(("closed_remote_many", closed))
            if failed:
                self.ui_queue.put(("error", "Some terminals failed to close:\n" + "\n".join(failed)))

        threading.Thread(target=worker, name="delete-many-worker", daemon=True).start()

    def process_ui_queue(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "connected":
                    _kind, tunnel, api, local_port = item
                    self.tunnel = tunnel
                    self.api = api
                    self.local_port = int(local_port)
                    self.set_status(f"Connected. Local tunnel port: {local_port}; WebSocket streaming enabled; default dir: {self.default_cwd or '<normal login>'}")
                    self.refresh_terminals()
                elif kind == "terminals":
                    self.render_terminal_list(item[1])
                    # Auto-open existing terminals for continuity.
                    for term in item[1]:
                        if term["id"] not in self.tabs:
                            self.attach_terminal(term)
                elif kind == "created":
                    term = item[1]
                    self.terminals[term["id"]] = term
                    self.refresh_terminals()
                    self.attach_terminal(term)
                elif kind == "closed_remote":
                    term_id = item[1]
                    if term_id in self.tabs:
                        tab = self.tabs.pop(term_id)
                        tab.detach()
                        self.notebook.forget(tab.frame)
                    self.refresh_terminals()
                elif kind == "closed_remote_many":
                    closed_ids = item[1]
                    for term_id in closed_ids:
                        if term_id in self.tabs:
                            tab = self.tabs.pop(term_id)
                            tab.detach()
                            self.notebook.forget(tab.frame)
                    self.set_status(f"Closed {len(closed_ids)} remote terminal(s).")
                    self.refresh_terminals()
                elif kind == "snapshot":
                    _kind, term_id, data = item
                    tab = self.tabs.get(term_id)
                    if tab is not None:
                        tab.set_snapshot(data)
                elif kind == "output":
                    _kind, term_id, data = item
                    tab = self.tabs.get(term_id)
                    if tab is not None:
                        tab.append_output(data)
                elif kind == "error":
                    self.show_error(str(item[1]))
                elif kind == "status":
                    self.set_status(str(item[1]))
        except queue.Empty:
            pass
        self.after(100, self.process_ui_queue)

    def on_close(self) -> None:
        # Only detach. Do not kill any remote terminal.
        for tab in list(self.tabs.values()):
            tab.detach()
        if self.tunnel is not None:
            self.tunnel.stop()
        self.local_port = None
        self.destroy()


def main() -> None:
    try:
        app = RemoteTerminalApp()
        app.mainloop()
    except Exception:
        err = traceback.format_exc()
        log_error(err)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Remote tmux Terminal crashed",
                f"The Windows client crashed. Error log:\n{ERROR_LOG_FILE}\n\n{err}",
            )
            root.destroy()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
