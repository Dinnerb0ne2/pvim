from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ActionSnapshot:
    lines: tuple[str, ...]
    cursor_x: int
    cursor_y: int
    line_ending: str


@dataclass(slots=True, frozen=True)
class ActionRecord:
    label: str
    before: ActionSnapshot
    after: ActionSnapshot


class HistoryStack:
    __slots__ = ("_items", "_index", "_max_actions")

    def __init__(self, *, max_actions: int = 400) -> None:
        self._items: list[ActionRecord] = []
        self._index = -1
        self._max_actions = max(20, int(max_actions))

    def set_limit(self, value: int) -> None:
        self._max_actions = max(20, int(value))
        self._trim()

    def clear(self) -> None:
        self._items.clear()
        self._index = -1

    def push(self, record: ActionRecord) -> None:
        if self._index < len(self._items) - 1:
            self._items = self._items[: self._index + 1]
        self._items.append(record)
        self._index = len(self._items) - 1
        self._trim()

    def undo(self) -> ActionRecord | None:
        if self._index < 0 or self._index >= len(self._items):
            return None
        record = self._items[self._index]
        self._index -= 1
        return record

    def redo(self) -> ActionRecord | None:
        next_index = self._index + 1
        if next_index < 0 or next_index >= len(self._items):
            return None
        self._index = next_index
        return self._items[self._index]

    def stats(self) -> tuple[int, int]:
        return len(self._items), self._index

    def _trim(self) -> None:
        overflow = len(self._items) - self._max_actions
        if overflow <= 0:
            return
        self._items = self._items[overflow:]
        self._index = max(-1, self._index - overflow)
