from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class TreeEntry:
    display: str
    relative_path: str


class FileTreeFeature:
    __slots__ = ("enabled", "visible", "entries", "selected", "scroll", "title")

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.visible = False
        self.entries: list[TreeEntry] = []
        self.selected = 0
        self.scroll = 0
        self.title = "EXPLORER"

    async def collect_paths(self, root: Path) -> list[str]:
        if not self.enabled:
            return []
        return await self._list_files(root)

    def apply_paths(self, paths: list[str]) -> None:
        self.entries = self._flatten_as_tree(paths)
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
        return self.entries[self.selected].relative_path

    def visible_entries(self, height: int) -> list[TreeEntry]:
        rows = max(1, height)
        end = min(len(self.entries), self.scroll + rows)
        return self.entries[self.scroll : end]

    async def _list_files(self, root: Path) -> list[str]:
        try:
            process = await asyncio.create_subprocess_exec(
                "fd",
                "--color",
                "never",
                "--type",
                "f",
                ".",
                str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await process.communicate()
            if process.returncode == 0:
                lines = stdout.decode("utf-8", errors="replace").splitlines()
                return [line.strip().replace("\\", "/") for line in lines if line.strip()]
        except OSError:
            pass

        files: list[str] = []
        for current_root, _dirs, names in root.walk():
            for name in sorted(names):
                relative = str((current_root / name).relative_to(root)).replace("\\", "/")
                files.append(relative)
        return files

    def _flatten_as_tree(self, paths: list[str]) -> list[TreeEntry]:
        if not paths:
            return []

        tree: dict[str, Any] = {}
        sorted_paths = sorted(paths, key=lambda item: item.lower())
        for rel_path in sorted_paths:
            parts = [part for part in rel_path.replace("\\", "/").split("/") if part]
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = rel_path

        output: list[TreeEntry] = []
        self._emit_tree(output, tree, depth=0, prefix_stack=[])
        return output

    def _emit_tree(
        self,
        output: list[TreeEntry],
        node: dict[str, Any],
        *,
        depth: int,
        prefix_stack: list[bool],
    ) -> None:
        keys = sorted(node.keys(), key=lambda item: item.lower())
        for index, key in enumerate(keys):
            is_last = index == len(keys) - 1
            branch = "└── " if is_last else "├── "
            prefix = ""
            if depth > 0:
                prefix = "".join("    " if done else "│   " for done in prefix_stack)
            payload = node[key]
            if isinstance(payload, dict):
                display = f"{prefix}{branch}{key}/"
                output.append(TreeEntry(display=display, relative_path=""))
                self._emit_tree(output, payload, depth=depth + 1, prefix_stack=[*prefix_stack, is_last])
            else:
                display = f"{prefix}{branch}{key}"
                output.append(TreeEntry(display=display, relative_path=str(payload)))
