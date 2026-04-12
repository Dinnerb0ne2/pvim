from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.core.config import AppConfig, DEFAULT_CONFIG
from src.ui.editor import PvimEditor
from src.ui.editor.modes import MODE_INSERT, MODE_NORMAL


class BufferEditorBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        config_path = self._root / "pvim.config.json"
        config_data = copy.deepcopy(DEFAULT_CONFIG)
        config_data["features"]["session"]["enabled"] = False
        config_data["features"]["swap"]["enabled"] = False
        config_data["features"]["notifications"]["enabled"] = False
        config_path.write_text(json.dumps(config_data), encoding="utf-8")
        self.config = AppConfig.load(config_path)
        self.editor = PvimEditor(None, self.config)

    def tearDown(self) -> None:
        self.editor._async_runtime.close()
        self._tmp.cleanup()

    def test_basic_insert_and_delete(self) -> None:
        self.editor.lines = ["abc"]
        self.editor.cx = 3
        self.editor.cy = 0
        self.editor._insert_text_multi("X")
        self.assertEqual(self.editor.lines, ["abcX"])
        self.editor._backspace()
        self.assertEqual(self.editor.lines, ["abc"])
        self.editor._delete_char()
        self.assertEqual(self.editor.lines, ["abc"])

    def test_large_text_insert_over_10000_lines(self) -> None:
        large_lines = [f"line-{index}" for index in range(12000)]
        self.editor.lines = large_lines
        self.editor.buffer.configure_piece_table(True)
        self.assertEqual(len(self.editor.lines), 12000)
        self.assertIn("line-11999", self.editor.buffer.text())

    def test_undo_redo_bounds(self) -> None:
        self.editor.lines = ["abc"]
        self.editor.cx = 3
        self.editor.cy = 0

        self.editor.handle_key("u")
        self.assertIn("Nothing to undo", self.editor.message)

        self.editor.mode = MODE_INSERT
        self.editor.handle_key("x")
        self.assertEqual(self.editor.lines, ["abcx"])

        self.editor.mode = MODE_NORMAL
        self.editor.handle_key("u")
        self.assertEqual(self.editor.lines, ["abc"])

        self.editor.handle_key("CTRL_Y")
        self.assertEqual(self.editor.lines, ["abcx"])

        self.editor.handle_key("CTRL_Y")
        self.assertIn("Nothing to redo", self.editor.message)

    def test_cursor_out_of_bounds_protection(self) -> None:
        self.editor.lines = ["a"]
        self.editor.cx = 999
        self.editor.cy = 999
        self.editor._ensure_cursor_bounds()
        self.assertEqual(self.editor.cy, 0)
        self.assertEqual(self.editor.cx, 1)

        self.editor.cx = -20
        self.editor.cy = -20
        self.editor._ensure_cursor_bounds()
        self.assertEqual(self.editor.cy, 0)
        self.assertEqual(self.editor.cx, 0)

    def test_empty_or_full_selection_delete_keeps_buffer_valid(self) -> None:
        self.editor.lines = [""]
        self.editor._delete_line_range(0, 0)
        self.assertEqual(self.editor.lines, [""])

        self.editor.lines = ["a", "b", "c"]
        self.editor._delete_line_range(0, 2)
        self.assertEqual(self.editor.lines, [""])
        self.assertEqual(self.editor.cy, 0)
        self.assertEqual(self.editor.cx, 0)


if __name__ == "__main__":
    unittest.main()
