from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class SwapPayload:
    file_path: str
    cursor_x: int
    cursor_y: int
    line_ending: str
    lines: list[str]


class EditorPersistence:
    __slots__ = ("_swap_root",)

    def __init__(self, *, swap_root: Path | None = None) -> None:
        self._swap_root = swap_root.resolve() if swap_root is not None else None

    def set_swap_directory(self, swap_root: Path | None) -> None:
        self._swap_root = swap_root.resolve() if swap_root is not None else None

    @staticmethod
    def _swap_file_name(file_path: Path) -> str:
        resolved = file_path.expanduser().resolve()
        normalized = str(resolved)
        if os.name == "nt":
            normalized = normalized.lower()
        digest = hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]
        base = "".join(ch if (ch.isalnum() or ch in {"-", "_", "."}) else "_" for ch in resolved.name)
        if not base:
            base = "buffer"
        return f"{base}.{digest}.pvim.swap.json"

    def swap_path(self, file_path: Path) -> Path:
        if self._swap_root is not None:
            self._swap_root.mkdir(parents=True, exist_ok=True)
            return self._swap_root / self._swap_file_name(file_path)
        suffix = file_path.suffix or ".txt"
        return file_path.with_suffix(f"{suffix}.pvim.swap")

    def write_swap(
        self,
        *,
        file_path: Path,
        lines: list[str],
        cursor_x: int,
        cursor_y: int,
        line_ending: str,
    ) -> None:
        payload = {
            "file_path": str(file_path),
            "cursor_x": int(cursor_x),
            "cursor_y": int(cursor_y),
            "line_ending": line_ending,
            "lines": list(lines),
        }
        swap_path = self.swap_path(file_path)
        swap_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def read_swap(self, file_path: Path) -> SwapPayload | None:
        swap_path = self.swap_path(file_path)
        if not swap_path.exists():
            return None
        try:
            loaded = json.loads(swap_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(loaded, dict):
            return None
        lines = loaded.get("lines", [])
        if not isinstance(lines, list):
            lines = []
        normalized_lines = [str(item) for item in lines]
        return SwapPayload(
            file_path=str(loaded.get("file_path", str(file_path))),
            cursor_x=int(loaded.get("cursor_x", 0)),
            cursor_y=int(loaded.get("cursor_y", 0)),
            line_ending=str(loaded.get("line_ending", "\n")),
            lines=normalized_lines,
        )

    def remove_swap(self, file_path: Path) -> None:
        swap_path = self.swap_path(file_path)
        if swap_path.exists():
            swap_path.unlink(missing_ok=True)

    def save_session(self, session_path: Path, payload: dict[str, Any]) -> None:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_session(self, session_path: Path) -> dict[str, Any] | None:
        if not session_path.exists():
            return None
        try:
            loaded = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(loaded, dict):
            return None
        return loaded
