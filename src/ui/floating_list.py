from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class FloatingList:
    title: str
    items: list[str] = field(default_factory=list)
    selected: int = 0
    scroll: int = 0
    footer: str = ""

    def set_items(self, items: list[str]) -> None:
        self.items = items
        self.selected = min(max(0, self.selected), max(0, len(items) - 1))
        self.scroll = min(max(0, self.scroll), max(0, len(items) - 1))

    def move_up(self) -> None:
        if self.selected > 0:
            self.selected -= 1
        if self.selected < self.scroll:
            self.scroll = self.selected

    def move_down(self, visible_rows: int) -> None:
        if self.selected < len(self.items) - 1:
            self.selected += 1
        max_scroll = max(0, len(self.items) - max(1, visible_rows))
        if self.selected >= self.scroll + max(1, visible_rows):
            self.scroll = min(max_scroll, self.selected - visible_rows + 1)

    def selected_item(self) -> str | None:
        if not self.items:
            return None
        return self.items[self.selected]
