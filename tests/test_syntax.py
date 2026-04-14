from __future__ import annotations

from pathlib import Path
import unittest

from src.core.config import AppConfig
from src.core.theme import load_theme
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


if __name__ == "__main__":
    unittest.main()
