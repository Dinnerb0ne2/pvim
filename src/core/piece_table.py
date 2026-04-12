from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Piece:
    source: str  # "original" or "add"
    start: int
    length: int


class PieceTable:
    """Immutable-buffer piece table for efficient inserts/deletes."""

    __slots__ = ("_original", "_add", "_pieces", "_length")

    def __init__(self, text: str = "") -> None:
        self._original = text
        self._add = ""
        self._pieces: list[Piece] = [Piece("original", 0, len(text))] if text else []
        self._length = len(text)

    def __len__(self) -> int:
        return self._length

    def reset(self, text: str) -> None:
        self._original = text
        self._add = ""
        self._pieces = [Piece("original", 0, len(text))] if text else []
        self._length = len(text)

    def to_string(self) -> str:
        if not self._pieces:
            return ""
        out: list[str] = []
        for piece in self._pieces:
            if piece.length <= 0:
                continue
            source = self._original if piece.source == "original" else self._add
            out.append(source[piece.start : piece.start + piece.length])
        return "".join(out)

    def insert(self, offset: int, text: str) -> None:
        if not text:
            return
        offset = max(0, min(offset, self._length))
        add_start = len(self._add)
        self._add += text
        new_piece = Piece("add", add_start, len(text))
        if not self._pieces:
            self._pieces.append(new_piece)
            self._length += len(text)
            return

        if offset == self._length:
            self._pieces.append(new_piece)
            self._length += len(text)
            return

        index, inner = self._locate(offset)
        current = self._pieces[index]
        if inner == 0:
            self._pieces.insert(index, new_piece)
        elif inner >= current.length:
            self._pieces.insert(index + 1, new_piece)
        else:
            left = Piece(current.source, current.start, inner)
            right = Piece(current.source, current.start + inner, current.length - inner)
            self._pieces[index : index + 1] = [left, new_piece, right]
        self._length += len(text)

    def delete(self, start: int, end: int) -> None:
        if start >= end or self._length <= 0:
            return
        start = max(0, min(start, self._length))
        end = max(0, min(end, self._length))
        if start >= end:
            return

        new_pieces: list[Piece] = []
        cursor = 0
        for piece in self._pieces:
            piece_start = cursor
            piece_end = cursor + piece.length
            cursor = piece_end

            if end <= piece_start or start >= piece_end:
                new_pieces.append(piece)
                continue

            keep_left = max(0, start - piece_start)
            keep_right = max(0, piece_end - end)
            if keep_left > 0:
                new_pieces.append(Piece(piece.source, piece.start, keep_left))
            if keep_right > 0:
                right_start = piece.start + piece.length - keep_right
                new_pieces.append(Piece(piece.source, right_start, keep_right))

        self._pieces = [piece for piece in new_pieces if piece.length > 0]
        self._length -= end - start
        if self._length < 0:
            self._length = 0

    def replace(self, start: int, end: int, text: str) -> None:
        self.delete(start, end)
        self.insert(start, text)

    def _locate(self, offset: int) -> tuple[int, int]:
        cursor = 0
        for index, piece in enumerate(self._pieces):
            next_cursor = cursor + piece.length
            if offset < next_cursor:
                return index, offset - cursor
            cursor = next_cursor
        return max(0, len(self._pieces) - 1), self._pieces[-1].length
