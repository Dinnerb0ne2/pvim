from __future__ import annotations

import os
from pathlib import Path

IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    "dist",
    "build",
}

IGNORED_SUFFIXES = {
    ".pyc",
}


class FileIndex:
    def __init__(self, root: Path, *, max_files: int) -> None:
        self._root = root.resolve()
        self._max_files = max(100, max_files)
        self._files: list[Path] = []
        self._loaded = False

    def refresh(self, *, force: bool = False) -> None:
        if self._loaded and not force:
            return

        files: list[Path] = []
        for current_root, dirs, names in os.walk(self._root):
            dirs[:] = [name for name in dirs if name not in IGNORED_DIRS and not name.startswith(".")]
            names.sort()
            for name in names:
                if name.startswith("."):
                    continue
                if Path(name).suffix.lower() in IGNORED_SUFFIXES:
                    continue
                full_path = Path(current_root) / name
                if not full_path.is_file():
                    continue
                files.append(full_path.relative_to(self._root))
                if len(files) >= self._max_files:
                    break
            if len(files) >= self._max_files:
                break

        self._files = sorted(files, key=lambda item: str(item).lower())
        self._loaded = True

    def list_files(self) -> list[Path]:
        self.refresh()
        return self._files
