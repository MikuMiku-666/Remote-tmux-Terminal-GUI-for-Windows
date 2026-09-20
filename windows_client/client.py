#!/usr/bin/env python3
"""v31 Windows client entry point; preserves the existing UI and features."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional

import tkinter.font as tkfont


def load_base_client() -> ModuleType:
    if getattr(sys, "frozen", False):
        source = Path(sys._MEIPASS) / "windows_client" / "base_client.py"
    else:
        source = Path(__file__).resolve().with_name("base_client.py")
    spec = importlib.util.spec_from_file_location("rterm_v31_base_client", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load existing Windows client from {source}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses and a few introspection tools expect the module to be present
    # while its source is executing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = load_base_client()
base.APP_VERSION = "v31: authoritative tmux cursor + adaptive terminal geometry"

OriginalTerminalTab = base.TerminalTab
_original_init = OriginalTerminalTab.__init__
_original_set_snapshot = OriginalTerminalTab.set_snapshot
_original_set_font_size = OriginalTerminalTab.set_font_size


def tab_init(self: Any, *args: Any, **kwargs: Any) -> None:
    self._v31_resize_after_id = None
    self._v31_last_geometry = None
    _original_init(self, *args, **kwargs)
    self.text.bind("<Configure>", lambda _event: schedule_geometry_sync(self), add="+")
    schedule_geometry_sync(self)


def schedule_geometry_sync(self: Any) -> None:
    if self._v31_resize_after_id is not None:
        try:
            self.text.after_cancel(self._v31_resize_after_id)
        except Exception:
            pass
    self._v31_resize_after_id = self.text.after(180, lambda: sync_geometry(self))


def sync_geometry(self: Any) -> None:
    self._v31_resize_after_id = None
    try:
        width = int(self.text.winfo_width())
        height = int(self.text.winfo_height())
        if width < 200 or height < 100:
            return
        font = tkfont.Font(font=self.text.cget("font"))
        cell_width = max(1, int(font.measure("M")))
        line_height = max(1, int(font.metrics("linespace")))
        cols = max(40, min(300, (width - 6) // cell_width))
        rows = max(10, min(100, (height - 6) // line_height))
        geometry = (cols, rows)
        if geometry == self._v31_last_geometry:
            return
        self._v31_last_geometry = geometry
        self._send_message(
            {"type": "resize", "cols": cols, "rows": rows},
            fallback_endpoint="resize",
        )
    except Exception:
        # Resizing is an enhancement; never let it interfere with attachment.
        return


def set_font_size(self: Any, size: int) -> None:
    _original_set_font_size(self, size)
    schedule_geometry_sync(self)


def set_snapshot(
    self: Any,
    data: str,
    cursor: Optional[Dict[str, Any]] = None,
    cmdline: Optional[Dict[str, Any]] = None,
) -> None:
    """Render a snapshot and make the remote tmux cursor authoritative."""
    if not isinstance(cursor, dict) or int(cursor.get("protocol", 0)) < 31:
        _original_set_snapshot(self, data, cursor, cmdline)
        return

    self.suppress_output_until = 0.0
    normalized = str(data).replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines:
        lines = [""]

    row = max(0, min(int(cursor.get("snapshot_row", 0)), len(lines) - 1))
    char_index = max(0, int(cursor.get("char_index", 0)))
    # Tk needs a real cell to paint a block cursor at EOL or on an empty row.
    # Add padding only to the authoritative cursor row, never to every line.
    if len(lines[row]) <= char_index:
        lines[row] += " " * (char_index + 1 - len(lines[row]))
    rendered = "\n".join(lines)

    self.text.configure(state="normal")
    self.text.delete("1.0", "end")
    self.text.insert("end", rendered)
    self._cursor_manually_placed = False
    self._user_trailing_spaces = 0
    self._last_snapshot_data = normalized
    self._set_local_cursor(f"{row + 1}.{char_index}")
    self._redraw_cursor_hint()
    self._start_cursor_blink()
    self.text.configure(state="disabled")
    self._scroll_bottom_keep_left()
    self._sync_bottom_input_from_remote(cmdline)


OriginalTerminalTab.__init__ = tab_init
OriginalTerminalTab.set_font_size = set_font_size
OriginalTerminalTab.set_snapshot = set_snapshot


if __name__ == "__main__":
    base.run_askpass_helper_if_requested()
    base.main()
