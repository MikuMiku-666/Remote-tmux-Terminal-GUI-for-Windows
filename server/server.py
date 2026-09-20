#!/usr/bin/env python3
"""v31 server entry point; reuses the existing server without modifying it."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict

from protocol import build_snapshot


def load_base_server() -> ModuleType:
    source = Path(__file__).resolve().with_name("base_server.py")
    spec = importlib.util.spec_from_file_location("rterm_v31_base_server", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load existing server from {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = load_base_server()


def capture_physical_rows(term_id: str, scrollback: int = 5000) -> list[str]:
    """Capture tmux physical rows, deliberately without -J line joining."""
    name = base.session_name(term_id)
    if not base.tmux_session_exists(name):
        raise base.HTTPException(status_code=404, detail="Terminal does not exist")
    result = base.run_cmd(
        ["tmux", "capture-pane", "-p", "-S", f"-{scrollback}", "-t", name],
        timeout=3.0,
    )
    normalized = result.stdout.replace("\r\n", "\n").replace("\r", "\n")
    rows = normalized.split("\n")
    # subprocess output has one record-separating LF. Remove only that LF;
    # blank physical pane rows before it remain intact for cursor-y alignment.
    if rows and rows[-1] == "":
        rows.pop()
    return rows or [""]


def capture_screen_state(term_id: str, scrollback: int = 5000) -> Dict[str, Any]:
    """Return an authoritative physical screen plus directly mappable cursor."""
    cursor = base.get_cursor_info(term_id)
    data, cursor_v31 = build_snapshot(capture_physical_rows(term_id, scrollback), cursor)
    return {
        "protocol": 31,
        "data": data,
        "cursor": cursor_v31,
        "cmdline": base.get_current_command_info(term_id, cursor),
    }


# Existing FastAPI handlers resolve this global through the imported module, so
# replacing it upgrades HTTP snapshots and WebSocket snapshots together.
base.capture_screen_state = capture_screen_state


if __name__ == "__main__":
    base.main()
