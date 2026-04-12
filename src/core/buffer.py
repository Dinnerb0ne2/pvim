from __future__ import annotations

from dataclasses import dataclass, field

from .piece_table import PieceTable


@dataclass(slots=True)
class Buffer:
    lines: list[str] = field(default_factory=lambda: [""])
    virtual_text: dict[int, list[str]] = field(default_factory=dict)
    dirty_lines: set[int] = field(default_factory=set)
    _dirty_all: bool = True
    _piece_table_enabled: bool = True
    _piece_table: PieceTable = field(default_factory=PieceTable)
    _piece_dirty: bool = True

    def configure_piece_table(self, enabled: bool) -> None:
        self._piece_table_enabled = enabled
        self._piece_dirty = True
        if enabled:
            self.sync_piece_table(force=True)

    def set_lines(self, lines: list[str]) -> None:
        self.lines = lines if lines else [""]
        self.virtual_text.clear()
        self._piece_dirty = True
        if self._piece_table_enabled:
            self.sync_piece_table(force=True)
        self.mark_all_dirty()

    def mark_dirty(self, line_index: int) -> None:
        if line_index < 0:
            return
        self.dirty_lines.add(line_index)
        self._piece_dirty = True

    def mark_all_dirty(self) -> None:
        self._dirty_all = True
        self._piece_dirty = True

    def consume_dirty(self) -> tuple[bool, set[int]]:
        dirty_all = self._dirty_all
        dirty = set(self.dirty_lines)
        self._dirty_all = False
        self.dirty_lines.clear()
        return dirty_all, dirty

    def sync_piece_table(self, *, force: bool = False) -> None:
        if not self._piece_table_enabled:
            return
        if not force and not self._piece_dirty:
            return
        text = "\n".join(self.lines)
        self._piece_table.reset(text)
        self._piece_dirty = False

    def piece_table_stats(self) -> dict[str, int | bool]:
        return {
            "enabled": self._piece_table_enabled,
            "length": len(self._piece_table),
            "line_count": len(self.lines),
            "dirty": self._piece_dirty,
        }

    def text(self) -> str:
        if self._piece_table_enabled:
            self.sync_piece_table()
            return self._piece_table.to_string()
        return "\n".join(self.lines)

    def set_virtual_text(self, line_index: int, chunks: list[str]) -> None:
        filtered = [item for item in chunks if item]
        if filtered:
            self.virtual_text[line_index] = filtered
        else:
            self.virtual_text.pop(line_index, None)
        self.mark_dirty(line_index)

    def add_virtual_text(self, line_index: int, text: str) -> None:
        if not text:
            return
        chunks = self.virtual_text.setdefault(line_index, [])
        chunks.append(text)
        self.mark_dirty(line_index)

    def clear_virtual_text(self, line_index: int | None = None) -> None:
        if line_index is None:
            for index in list(self.virtual_text.keys()):
                self.mark_dirty(index)
            self.virtual_text.clear()
            return
        if line_index in self.virtual_text:
            self.virtual_text.pop(line_index, None)
            self.mark_dirty(line_index)

    def get_virtual_text(self, line_index: int) -> list[str]:
        return self.virtual_text.get(line_index, [])
