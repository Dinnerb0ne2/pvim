from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.core.config import AppConfig, DEFAULT_CONFIG
from src.ui.editor import PvimEditor
from src.ui.editor.modes import MODE_INSERT
from src.ui_grid import AbstractUI


class MockUI(AbstractUI):
    def __init__(self, *, width: int = 100, height: int = 30) -> None:
        self.width = width
        self.height = height
        self.update_calls: list[tuple[list[str], list[int] | None]] = []
        self.cursor_calls: list[tuple[int, int]] = []
        self.flush_calls = 0
        self.clear_calls = 0

    def update_grid(self, rows: list[str], *, dirty_rows: list[int] | None = None) -> None:
        self.update_calls.append((list(rows), list(dirty_rows) if dirty_rows is not None else None))

    def flush(self) -> None:
        self.flush_calls += 1

    def set_cursor(self, row: int, col: int) -> None:
        self.cursor_calls.append((row, col))

    def get_size(self) -> tuple[int, int]:
        return self.width, self.height

    def clear(self) -> None:
        self.clear_calls += 1


class EditorUiDecouplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        config_path = self._root / "pvim.config.json"
        config_data = copy.deepcopy(DEFAULT_CONFIG)
        config_data["features"]["session"]["enabled"] = False
        config_data["features"]["swap"]["enabled"] = False
        config_data["features"]["notifications"]["enabled"] = False
        config_data["features"]["tabline"]["enabled"] = False
        config_data["features"]["winbar"]["enabled"] = False
        config_data["features"]["sidebar"]["enabled"] = False
        config_path.write_text(json.dumps(config_data), encoding="utf-8")
        self.config = AppConfig.load(config_path)
        self.ui = MockUI()
        self.editor = PvimEditor(None, self.config, ui=self.ui)

    def tearDown(self) -> None:
        self.editor.shutdown()
        self._tmp.cleanup()

    def test_render_routes_grid_and_cursor_to_ui(self) -> None:
        self.editor.lines = ["alpha", "beta"]
        self.editor.cy = 1
        self.editor.cx = 2

        self.editor.render()

        self.assertGreaterEqual(len(self.ui.update_calls), 1)
        self.assertGreaterEqual(len(self.ui.cursor_calls), 1)
        self.assertGreaterEqual(self.ui.flush_calls, 1)
        rows, dirty_rows = self.ui.update_calls[-1]
        self.assertTrue(any("alpha" in row for row in rows))
        self.assertIsNotNone(dirty_rows)
        self.assertTrue(all(isinstance(item, int) for item in dirty_rows or []))

    def test_edit_action_produces_grid_update_and_cursor_move(self) -> None:
        self.editor.lines = ["abc"]
        self.editor.cy = 0
        self.editor.cx = 3
        self.editor.mode = MODE_INSERT

        self.editor.handle_key("X")
        self.editor.render()

        self.assertEqual(self.editor.lines[0], "abcX")
        self.assertGreaterEqual(len(self.ui.update_calls), 1)
        rows, _dirty_rows = self.ui.update_calls[-1]
        self.assertTrue(any("abcX" in row for row in rows))
        cursor_row, cursor_col = self.ui.cursor_calls[-1]
        self.assertGreater(cursor_row, 0)
        self.assertGreater(cursor_col, 0)


if __name__ == "__main__":
    unittest.main()
