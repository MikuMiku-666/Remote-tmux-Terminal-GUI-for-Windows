import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from protocol import build_snapshot, display_column_to_index


class CursorProtocolTests(unittest.TestCase):
    def test_ascii_column(self):
        self.assertEqual(display_column_to_index("abc", 2), 2)

    def test_wide_character_column(self):
        self.assertEqual(display_column_to_index("a中b", 1), 1)
        self.assertEqual(display_column_to_index("a中b", 3), 2)

    def test_combining_character(self):
        self.assertEqual(display_column_to_index("e\u0301x", 1), 2)

    def test_emoji_zwj_sequence(self):
        self.assertEqual(display_column_to_index("👩‍💻x", 2), 3)

    def test_cursor_row_maps_from_visible_pane_to_scrollback(self):
        rows = ["history", "prompt", "", ""]
        cursor = {"x": 6, "y": 0, "width": 80, "height": 3}
        data, enriched = build_snapshot(rows, cursor)
        self.assertEqual(data, "history\nprompt\n\n")
        self.assertIsNotNone(enriched)
        self.assertEqual(enriched["snapshot_row"], 1)
        self.assertEqual(enriched["char_index"], 6)


if __name__ == "__main__":
    unittest.main()
