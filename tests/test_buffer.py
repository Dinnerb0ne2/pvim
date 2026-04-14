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

    def test_ctrl_r_redo_alias(self) -> None:
        self.editor.lines = ["abc"]
        self.editor.cx = 3
        self.editor.cy = 0
        self.editor.mode = MODE_INSERT
        self.editor.handle_key("x")
        self.editor.mode = MODE_NORMAL
        self.editor.handle_key("u")
        self.assertEqual(self.editor.lines, ["abc"])
        self.editor.handle_key("CTRL_R")
        self.assertEqual(self.editor.lines, ["abcx"])

    def test_undo_tree_branch_switch_with_g_minus_and_g_plus(self) -> None:
        self.editor.lines = ["abc"]
        self.editor.cx = 3
        self.editor.cy = 0
        self.editor.mode = MODE_INSERT
        self.editor.handle_key("x")
        self.editor.mode = MODE_NORMAL
        self.editor.handle_key("u")
        self.editor.mode = MODE_INSERT
        self.editor.handle_key("y")
        self.editor.mode = MODE_NORMAL
        self.assertEqual(self.editor.lines, ["abcy"])

        self.editor.handle_key("g")
        self.editor.handle_key("-")
        self.assertEqual(self.editor.lines, ["abcx"])

        self.editor.handle_key("g")
        self.editor.handle_key("+")
        self.assertEqual(self.editor.lines, ["abcy"])

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

    def test_jump_history_back_and_forward(self) -> None:
        self.editor.lines = ["def foo():", "    pass", "", "foo()"]
        self.editor.cy = 3
        self.editor.cx = 1

        self.editor._goto_definition()
        self.assertEqual(self.editor.cy, 0)

        self.assertTrue(self.editor._jump_back())
        self.assertEqual(self.editor.cy, 3)

        self.assertTrue(self.editor._jump_forward())
        self.assertEqual(self.editor.cy, 0)

    def test_matching_bracket_jump(self) -> None:
        self.editor.lines = ["if (a[0] + b):"]
        self.editor.cy = 0
        self.editor.cx = 3

        self.assertTrue(self.editor._jump_to_matching_bracket())
        self.assertEqual((self.editor.cy, self.editor.cx), (0, 12))

        self.assertTrue(self.editor._jump_to_matching_bracket())
        self.assertEqual((self.editor.cy, self.editor.cx), (0, 3))

    def test_project_replace_all_updates_workspace_files(self) -> None:
        first = self._root / "a.py"
        second = self._root / "b.py"
        first.write_text("TODO\nkeep\n", encoding="utf-8")
        second.write_text("x TODO y\n", encoding="utf-8")

        self.assertTrue(self.editor.open_project(self._root, force=True))
        self.assertTrue(self.editor._replace_all_project("TODO", "DONE"))

        self.assertIn("DONE", first.read_text(encoding="utf-8"))
        self.assertIn("DONE", second.read_text(encoding="utf-8"))

    def test_sidebar_defaults_to_off_for_file_and_on_for_project(self) -> None:
        target = self._root / "demo.py"
        target.write_text("print('ok')\n", encoding="utf-8")

        self.assertFalse(self.editor.show_sidebar)
        self.assertTrue(self.editor.open_file(target, force=True))
        self.assertFalse(self.editor.show_sidebar)
        self.assertTrue(self.editor.open_project(self._root, force=True))
        self.assertTrue(self.editor.show_sidebar)

    def test_session_restore_is_disabled_by_default_on_startup(self) -> None:
        runtime_root = self._root / "runtime"
        restored_file = self._root / "restored.py"
        restored_file.write_text("x = 1\n", encoding="utf-8")

        config_path = self._root / "session-default.config.json"
        config_data = copy.deepcopy(DEFAULT_CONFIG)
        config_data["runtime"]["directory"] = str(runtime_root)
        config_data["features"]["swap"]["enabled"] = False
        config_data["features"]["notifications"]["enabled"] = False
        config_path.write_text(json.dumps(config_data), encoding="utf-8")
        config = AppConfig.load(config_path)

        session_payload = {
            "current_file": str(restored_file),
            "cursor_x": 0,
            "cursor_y": 0,
            "tabs": ["restored.py"],
            "tab_index": 0,
            "workspace_root": str(self._root),
        }
        config.session_file().parent.mkdir(parents=True, exist_ok=True)
        config.session_file().write_text(json.dumps(session_payload), encoding="utf-8")

        startup_editor = PvimEditor(None, config)
        try:
            self.assertIsNone(startup_editor.file_path)
            self.assertEqual(startup_editor.lines, [""])
        finally:
            startup_editor._async_runtime.close()

    def test_incremental_search_preview_history_and_cancel_restore(self) -> None:
        self.editor.lines = ["alpha beta", "beta alpha", "omega"]
        self.editor.cy = 0
        self.editor.cx = 0
        self.editor.mode = MODE_NORMAL

        self.editor.handle_key("/")
        self.editor.handle_key("b")
        self.editor.handle_key("e")
        self.editor.handle_key("t")
        self.editor.handle_key("a")
        self.assertEqual((self.editor.cy, self.editor.cx), (0, 6))
        self.editor.handle_key("ENTER")
        self.assertIn("Found: beta", self.editor.message)
        self.assertIn("beta", list(self.editor._search_history))
        self.editor.handle_key("n")
        self.assertEqual((self.editor.cy, self.editor.cx), (1, 0))

        self.editor.handle_key("/")
        self.editor.handle_key("UP")
        self.assertEqual(self.editor.command_text, "find beta")
        self.editor.handle_key("ESC")

        self.editor.cy = 1
        self.editor.cx = 0
        self.editor.handle_key("/")
        self.editor.handle_key("a")
        self.assertEqual((self.editor.cy, self.editor.cx), (1, 3))
        self.editor.handle_key("ESC")
        self.assertEqual((self.editor.cy, self.editor.cx), (1, 0))

    def test_autocmd_bufreadpost_and_bufwritepre(self) -> None:
        cfg = self.editor.config.data
        autocmd = cfg.setdefault("features", {}).setdefault("autocmds", {})
        if isinstance(autocmd, dict):
            autocmd["events"] = {
                "bufreadpost": ["set nonumber"],
                "bufwritepre": ["set number"],
            }
        self.editor._apply_runtime_config()

        target = self._root / "auto.py"
        target.write_text("x = 1\n", encoding="utf-8")

        self.assertTrue(self.editor.open_file(target, force=True))
        self.assertFalse(self.editor.show_line_numbers)
        self.assertTrue(self.editor.save_file())
        self.assertTrue(self.editor.show_line_numbers)

    def test_scoped_var_commands(self) -> None:
        self.editor.execute_command("var set g:theme nvim-tokyonight")
        self.editor.execute_command("var get g:theme")
        self.assertIn("g:theme=nvim-tokyonight", self.editor.message)

        self.editor.execute_command("var set b:marker active")
        self.editor.execute_command("var get b:marker")
        self.assertIn("b:marker=active", self.editor.message)

    def test_clipboard_copy_and_paste_uses_runtime_cache(self) -> None:
        cfg = self.editor.config.data
        clipboard = cfg.setdefault("features", {}).setdefault("clipboard", {})
        if isinstance(clipboard, dict):
            clipboard["enabled"] = False
        self.editor.lines = ["abc"]
        self.editor.cy = 0
        self.editor.cx = 3

        self.editor.execute_command("clip copy XYZ")
        self.editor.execute_command("clip paste")
        self.assertEqual(self.editor.lines[0], "abcXYZ")

    def test_quickfix_from_grep_and_next(self) -> None:
        first = self._root / "qa.py"
        second = self._root / "qb.py"
        first.write_text("TODO one\n", encoding="utf-8")
        second.write_text("TODO two\n", encoding="utf-8")
        self.assertTrue(self.editor.open_project(self._root, force=True))

        self.editor.execute_command("quickfix fromgrep TODO")
        self.assertGreaterEqual(len(self.editor._quickfix_items), 1)
        self.editor.execute_command("quickfix next")
        self.assertIsNotNone(self.editor.file_path)

    def test_dap_breakpoint_commands(self) -> None:
        target = self._root / "debug_target.py"
        target.write_text("print('x')\n", encoding="utf-8")
        self.assertTrue(self.editor.open_file(target, force=True))

        self.editor.execute_command("dap break add 1")
        key = str(target.resolve())
        self.assertIn(1, self.editor._dap_breakpoints.get(key, set()))

        self.editor.execute_command("dap break remove 1")
        self.assertNotIn(1, self.editor._dap_breakpoints.get(key, set()))


if __name__ == "__main__":
    unittest.main()
