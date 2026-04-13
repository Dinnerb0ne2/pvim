from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import time
import unittest

from src.features.fuzzy import fuzzy_filter, fuzzy_score
from src.features.modules.file_tree import FileTreeFeature


class FeatureBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_tree_scandir_filtering_and_speed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(1000):
                folder = root / f"pkg_{index // 50}"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"file_{index}.txt").write_text("x", encoding="utf-8")

            ignored = root / "node_modules"
            ignored.mkdir(parents=True, exist_ok=True)
            (ignored / "skip.js").write_text("x", encoding="utf-8")
            (root / "bad.pyc").write_text("x", encoding="utf-8")

            feature = FileTreeFeature(enabled=True)
            start = time.perf_counter()
            paths = await feature.collect_paths(root)
            elapsed = time.perf_counter() - start

            self.assertLess(elapsed, 0.5)
            self.assertEqual(len(paths), 1000)
            self.assertFalse(any(path.startswith("node_modules/") for path in paths))
            self.assertFalse(any(path.endswith(".pyc") for path in paths))

    def test_fuzzy_score_ranks_expected_match_higher(self) -> None:
        good = Path("neovim/init.lua")
        weak = Path("anvim")
        files = [weak, good]
        ranked = fuzzy_filter(files, "nvim", limit=20)

        self.assertEqual(ranked[0], good)
        self.assertGreater(fuzzy_score(str(good), "nvim") or 0, fuzzy_score(str(weak), "nvim") or 0)


if __name__ == "__main__":
    unittest.main()
