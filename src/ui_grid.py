from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Cell:
    char: str = " "
    fg: str | None = None
    bg: str | None = None
    bold: bool | None = None
    underline: bool | None = None


class AbstractUI(ABC):
    @abstractmethod
    def update_grid(self, rows: list[str], *, dirty_rows: list[int] | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def flush(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_cursor(self, row: int, col: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_size(self) -> tuple[int, int]:
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> None:
        raise NotImplementedError
