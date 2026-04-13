from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

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

IGNORED_FILE_SUFFIXES = {
    ".pyc",
}


@dataclass(slots=True, frozen=True)
class TreeEntry:
    display: str
    relative_path: str
    node_path: str
    is_dir: bool


class FileTreeFeature:
    __slots__ = (
        "enabled",
        "visible",
        "entries",
        "selected",
        "scroll",
        "title",
        "unicode_art",
        "_collapsed_dirs",
        "_raw_paths",
    )

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.visible = False
        self.entries: list[TreeEntry] = []
        self.selected = 0
        self.scroll = 0
        self.title = "EXPLORER"
        self.unicode_art = True
        self._collapsed_dirs: set[str] = set()
        self._raw_paths: list[str] = []

    async def collect_paths(self, root: Path) -> list[str]:
        if not self.enabled:
            return []
        return await self._list_files(root)

    def apply_paths(self, paths: list[str]) -> None:
        self._raw_paths = sorted({item for item in paths if item.strip()}, key=lambda item: item.lower())
        self.entries = self._flatten_as_tree(self._raw_paths, self._collapsed_dirs)
        self.selected = min(self.selected, max(0, len(self.entries) - 1))
        self.scroll = min(self.scroll, max(0, len(self.entries) - 1))

    def open(self) -> None:
        if self.enabled:
            self.visible = True

    def close(self) -> None:
        self.visible = False

    def move_up(self) -> None:
        if self.selected > 0:
            self.selected -= 1
        if self.selected < self.scroll:
            self.scroll = self.selected

    def move_down(self, visible_rows: int) -> None:
        if self.selected < len(self.entries) - 1:
            self.selected += 1
        if self.selected >= self.scroll + max(1, visible_rows):
            self.scroll = self.selected - visible_rows + 1

    def selected_path(self) -> str | None:
        if not self.entries:
            return None
        entry = self.entries[self.selected]
        if entry.is_dir:
            return None
        return entry.relative_path

    def toggle_selected_directory(self) -> bool:
        if not self.entries:
            return False
        entry = self.entries[self.selected]
        if not entry.is_dir or not entry.node_path:
            return False
        if entry.node_path in self._collapsed_dirs:
            self._collapsed_dirs.remove(entry.node_path)
        else:
            self._collapsed_dirs.add(entry.node_path)

        current_node = entry.node_path
        self.entries = self._flatten_as_tree(self._raw_paths, self._collapsed_dirs)
        for index, candidate in enumerate(self.entries):
            if candidate.node_path == current_node:
                self.selected = index
                break
        self.selected = min(self.selected, max(0, len(self.entries) - 1))
        self.scroll = min(self.scroll, max(0, len(self.entries) - 1))
        return True

    def visible_entries(self, height: int) -> list[TreeEntry]:
        rows = max(1, height)
        end = min(len(self.entries), self.scroll + rows)
        return self.entries[self.scroll : end]

    async def _list_files(self, root: Path) -> list[str]:
        return await asyncio.to_thread(self._scan_files, root.resolve())

    def _scan_files(self, root: Path) -> list[str]:
        files: list[str] = []
        root_path = str(root)
        stack: list[str] = [root_path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries_iter:
                    files_batch: list[str] = []
                    dirs_batch: list[str] = []
                    for entry in entries_iter:
                        name = entry.name
                        if entry.is_dir(follow_symlinks=False):
                            if name.startswith(".") or name in IGNORED_DIRS:
                                continue
                            dirs_batch.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if name.startswith("."):
                            continue
                        suffix = Path(name).suffix.lower()
                        if suffix in IGNORED_FILE_SUFFIXES:
                            continue
                        relative = os.path.relpath(entry.path, root_path).replace("\\", "/")
                        files_batch.append(relative)
                    files.extend(sorted(files_batch, key=lambda item: item.lower()))
                    dirs_batch.sort(key=lambda item: str(item).lower(), reverse=True)
                    stack.extend(dirs_batch)
            except OSError:
                continue
        return sorted(files, key=lambda item: item.lower())

    def _flatten_as_tree(self, paths: list[str], collapsed: set[str] | None = None) -> list[TreeEntry]:
        if not paths:
            return []
        collapsed_dirs = collapsed or set()

        tree: dict[str, Any] = {}
        sorted_paths = sorted(paths, key=lambda item: item.lower())
        for rel_path in sorted_paths:
            parts = [part for part in rel_path.replace("\\", "/").split("/") if part]
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = rel_path

        output: list[TreeEntry] = []
        self._emit_tree(output, tree, depth=0, prefix_stack=[], collapsed=collapsed_dirs, prefix_path=[])
        return output

    def _emit_tree(
        self,
        output: list[TreeEntry],
        node: dict[str, Any],
        *,
        depth: int,
        prefix_stack: list[bool],
        collapsed: set[str],
        prefix_path: list[str],
    ) -> None:
        keys = sorted(node.keys(), key=lambda item: item.lower())
        for index, key in enumerate(keys):
            is_last = index == len(keys) - 1
            if self.unicode_art:
                branch = "└── " if is_last else "├── "
            else:
                branch = "`-- " if is_last else "|-- "
            prefix = ""
            if depth > 0:
                if self.unicode_art:
                    prefix = "".join("    " if done else "│   " for done in prefix_stack)
                else:
                    prefix = "".join("    " if done else "|   " for done in prefix_stack)
            payload = node[key]
            if isinstance(payload, dict):
                node_path = "/".join([*prefix_path, key])
                collapsed_here = node_path in collapsed
                suffix = "/ [..]" if collapsed_here else "/"
                display = f"{prefix}{branch}{key}{suffix}"
                output.append(TreeEntry(display=display, relative_path="", node_path=node_path, is_dir=True))
                if not collapsed_here:
                    self._emit_tree(
                        output,
                        payload,
                        depth=depth + 1,
                        prefix_stack=[*prefix_stack, is_last],
                        collapsed=collapsed,
                        prefix_path=[*prefix_path, key],
                    )
            else:
                display = f"{prefix}{branch}{key}"
                file_path = str(payload)
                output.append(TreeEntry(display=display, relative_path=file_path, node_path=file_path, is_dir=False))
