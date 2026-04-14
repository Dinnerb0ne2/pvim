from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.core.config import AppConfig, DEFAULT_CONFIG


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        config_path = self._root / "pvim.config.json"
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["features"]["vscode_shortcuts"]["bindings"]["format_code"] = "F8"
        payload["features"]["vscode_shortcuts"]["filetype_bindings"] = {
            "python": {"format_code": "F9"},
        }
        payload["features"]["syntax_highlighting"]["extra_language_map_files"] = [
            "syntax\\extra-languages.json"
        ]
        payload["features"]["session"]["profiles_directory"] = ".sessions"
        payload["features"]["config_reload"]["interval_seconds"] = 2.5
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        self.config = AppConfig.load(config_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shortcut_for_language_prefers_filetype_binding(self) -> None:
        value = self.config.shortcut_for_language("format_code", "F8", "python")
        fallback = self.config.shortcut_for_language("format_code", "F8", "go")
        self.assertEqual(value, "F9")
        self.assertEqual(fallback, "F8")

    def test_syntax_language_map_files_includes_extra(self) -> None:
        files = self.config.syntax_language_map_files()
        self.assertGreaterEqual(len(files), 2)
        self.assertTrue(any(path.name == "languages.json" for path in files))
        self.assertTrue(any(path.name == "extra-languages.json" for path in files))

    def test_session_and_reload_settings(self) -> None:
        self.assertEqual(self.config.session_profiles_directory().name, ".sessions")
        self.assertAlmostEqual(self.config.config_reload_interval_seconds(), 2.5)

    def test_lsp_language_map_normalizes_extension(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["features"]["lsp"]["language_id_map"] = {"py": "python"}
        config_path = self._root / "custom-lsp.config.json"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        custom = AppConfig.load(config_path)
        mapping = custom.lsp_language_id_map()
        self.assertEqual(mapping.get(".py"), "python")


if __name__ == "__main__":
    unittest.main()
