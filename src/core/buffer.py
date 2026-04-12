from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Buffer:
    lines: list[str] = field(default_factory=lambda: [""])
    virtual_text: dict[int, list[str]] = field(default_factory=dict)
    dirty_lines: set[int] = field(default_factory=set)
    _dirty_all: bool = True

    def set_lines(self, lines: list[str]) -> None:
        self.lines = lines if lines else [""]
        self.virtual_text.clear()
        self.mark_all_dirty()

    def mark_dirty(self, line_index: int) -> None:
        if line_index < 0:
            return
        self.dirty_lines.add(line_index)

    def mark_all_dirty(self) -> None:
        self._dirty_all = True

    def consume_dirty(self) -> tuple[bool, set[int]]:
        dirty_all = self._dirty_all
        dirty = set(self.dirty_lines)
        self._dirty_all = False
        self.dirty_lines.clear()
        return dirty_all, dirty

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
