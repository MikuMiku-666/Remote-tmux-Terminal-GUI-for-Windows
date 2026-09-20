"""Pure helpers for the v31 authoritative cursor snapshot protocol."""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, Iterable


def cell_width(char: str) -> int:
    """Return the number of terminal cells occupied by one Unicode character."""
    try:
        from wcwidth import wcwidth  # type: ignore

        width = wcwidth(char)
        return 1 if width < 0 else width
    except ImportError:
        if not char or unicodedata.combining(char):
            return 0
        if char in {"\ufe0e", "\ufe0f", "\u200d"}:
            return 0
        return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_column_to_index(text: str, column: int) -> int:
    """Convert a tmux display-cell column into a Python/Tk character index."""
    target = max(0, int(column))
    used = 0
    index = 0
    while index < len(text):
        end = index + 1

        def is_extension(char: str) -> bool:
            codepoint = ord(char)
            return bool(
                unicodedata.combining(char)
                or char in {"\ufe0e", "\ufe0f"}
                or 0x1F3FB <= codepoint <= 0x1F3FF
            )

        while end < len(text) and is_extension(text[end]):
            end += 1
        while end < len(text) and text[end] == "\u200d":
            end += 1
            if end < len(text):
                end += 1
            while end < len(text) and is_extension(text[end]):
                end += 1

        cluster = text[index:end]
        try:
            from wcwidth import wcswidth  # type: ignore

            width = wcswidth(cluster)
        except ImportError:
            width = sum(cell_width(char) for char in cluster)
        if width < 0:
            width = sum(cell_width(char) for char in cluster)
        # Old wcwidth releases count every pictograph in a ZWJ cluster. A
        # terminal renders the cluster as one glyph whose width is the widest
        # joined component (normally two cells).
        if "\u200d" in cluster:
            width = max((cell_width(char) for char in cluster if char != "\u200d"), default=0)
        if used + width > target:
            return index
        used += width
        index = end
    return index


def build_snapshot(
    rows: Iterable[str],
    cursor: Dict[str, int] | None,
) -> tuple[str, Dict[str, Any] | None]:
    """Build physical-row text and cursor metadata understood by the v31 client."""
    physical_rows = list(rows)
    if not physical_rows:
        physical_rows = [""]
    data = "\n".join(physical_rows)
    if not cursor:
        return data, None

    height = max(1, int(cursor.get("height", 1)))
    visible_y = max(0, min(int(cursor.get("y", 0)), height - 1))
    snapshot_row = max(0, len(physical_rows) - height + visible_y)
    snapshot_row = min(snapshot_row, len(physical_rows) - 1)
    display_col = max(0, int(cursor.get("x", 0)))
    row_text = physical_rows[snapshot_row]

    enriched: Dict[str, Any] = dict(cursor)
    enriched.update(
        {
            "protocol": 31,
            "snapshot_row": snapshot_row,
            "display_col": display_col,
            "char_index": display_column_to_index(row_text, display_col),
        }
    )
    return data, enriched
