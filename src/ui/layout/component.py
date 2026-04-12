from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(slots=True)
class LayoutContext:
    width: int
    height: int
    mode: str
    file_name: str
    row: int
    col: int
    branch: str


class UIComponent:
    __slots__ = ("name", "enabled")

    def __init__(self, name: str, *, enabled: bool = True) -> None:
        self.name = name
        self.enabled = enabled

    def render(self, context: LayoutContext) -> list[str]:
        return []
