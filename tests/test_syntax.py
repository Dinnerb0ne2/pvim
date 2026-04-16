from __future__ import annotations

from pathlib import Path
import re
import unittest

from src.core.config import AppConfig
from src.core.theme import load_theme
from src.features.incremental_syntax import IncrementalSyntaxModel
from src.features.syntax import SyntaxManager


class SyntaxManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        config_path = project_root / "pvim.config.json"
        self.config = AppConfig.load(config_path)
        self.syntax = SyntaxManager(self.config)
        self.theme = load_theme(self.config.theme_file())

    def test_python_tokenize_highlight_handles_inline_comment_and_strings(self) -> None:
        profile = self.syntax.profile_for_file(Path("demo.py"))
        line = 'def render(): return "http://x#y"  # trailing comment'
        rendered = self.syntax.highlight_line(line, profile, self.theme, "")
        self.assertIn('"http://x#y"', rendered)
        self.assertIn("# trailing comment", rendered)

    def test_highlight_cache_and_regex_rule_loading(self) -> None:
        profile = self.syntax.profile_for_file(Path("demo.sh"))
        first = self.syntax.highlight_line("$HOME && echo", profile, self.theme, "")
        second = self.syntax.highlight_line("$HOME && echo", profile, self.theme, "")
        self.assertEqual(first, second)
        self.assertEqual(self.syntax._token_style_from_regex_rules(profile, "$HOME"), "builtin")  # type: ignore[attr-defined]

    def test_python_highlight_does_not_duplicate_text(self) -> None:
        profile = self.syntax.profile_for_file(Path("demo.py"))
        line = "value = call(arg)"
        rendered = self.syntax.highlight_line(line, profile, self.theme, "")
        plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        self.assertEqual(plain, line)

    def test_python_highlight_preserves_text_for_edge_cases(self) -> None:
        profile = self.syntax.profile_for_file(Path("demo.py"))
        lines = [
            "x = {'a': [1, 2, (3, 4)]}",
            "print(\"http://x#y\")  # trailing",
            "value = call(arg[0]) + other({'k': '(v)'})",
            "unfinished = \"abc",
            "f\"name={user.name}\"",
        ]
        for line in lines:
            rendered = self.syntax.highlight_line(line, profile, self.theme, "")
            plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
            self.assertEqual(plain, line)

    def test_incremental_syntax_model_reports_incremental_parse_window(self) -> None:
        model = IncrementalSyntaxModel()
        lines = [
            "def outer():",
            "    if ready:",
            "        run()",
            "    return 1",
        ]
        first = model.update(lines)
        self.assertTrue(first.changed)
        self.assertEqual(first.parsed_from, 0)
        self.assertEqual(first.parsed_lines, len(lines))

        second = model.update(lines)
        self.assertFalse(second.changed)
        self.assertEqual(second.parsed_lines, 0)

        updated = list(lines)
        updated[-1] = "    return 2"
        third = model.update(updated)
        self.assertTrue(third.changed)
        self.assertGreaterEqual(third.parsed_from, 0)
        self.assertLessEqual(third.parsed_from, len(updated) - 1)
        self.assertGreaterEqual(third.parsed_lines, 1)

    def test_incremental_syntax_model_builds_indent_and_brace_folds(self) -> None:
        model = IncrementalSyntaxModel()
        lines = [
            "def fold_me():",
            "    if flag:",
            "        pass",
            "",
            "config = {",
            "  'k': 1,",
            "}",
        ]
        model.update(lines)
        folds = model.folds()
        self.assertTrue(any(item.kind == "indent" and item.start_line == 0 for item in folds))
        self.assertTrue(any(item.kind.startswith("brace:") and item.start_line == 4 for item in folds))


if __name__ == "__main__":
    unittest.main()
