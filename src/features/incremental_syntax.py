from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSE_TO_OPEN = {")": "(", "]": "[", "}": "{"}


@dataclass(frozen=True, slots=True)
class FoldRange:
    start_line: int
    end_line: int
    kind: str


@dataclass(frozen=True, slots=True)
class ParseSummary:
    changed: bool
    changed_start: int
    changed_end: int
    parsed_from: int
    parsed_lines: int


class IncrementalSyntaxModel:
    __slots__ = (
        "_lines",
        "_line_indents",
        "_depth_before",
        "_depth_after",
        "_folds",
    )

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._line_indents: list[int] = []
        self._depth_before: list[int] = []
        self._depth_after: list[int] = []
        self._folds: tuple[FoldRange, ...] = ()

    def update(self, lines: Sequence[str]) -> ParseSummary:
        new_lines = [str(item) for item in lines]
        old_lines = self._lines
        if old_lines == new_lines:
            return ParseSummary(
                changed=False,
                changed_start=-1,
                changed_end=-1,
                parsed_from=-1,
                parsed_lines=0,
            )

        old_len = len(old_lines)
        new_len = len(new_lines)
        min_len = min(old_len, new_len)

        prefix = 0
        while prefix < min_len and old_lines[prefix] == new_lines[prefix]:
            prefix += 1

        suffix = 0
        while suffix < (min_len - prefix):
            if old_lines[old_len - 1 - suffix] != new_lines[new_len - 1 - suffix]:
                break
            suffix += 1

        changed_start = prefix
        changed_end = max(changed_start, new_len - suffix - 1) if new_lines else -1
        parse_from = max(0, changed_start - 1)

        new_indents = [0] * new_len
        new_depth_before = [0] * new_len
        new_depth_after = [0] * new_len

        copy_limit = min(parse_from, old_len)
        for index in range(copy_limit):
            new_indents[index] = self._line_indents[index]
            new_depth_before[index] = self._depth_before[index]
            new_depth_after[index] = self._depth_after[index]

        depth = new_depth_after[parse_from - 1] if parse_from > 0 and parse_from - 1 < len(new_depth_after) else 0
        for index in range(parse_from, new_len):
            line = new_lines[index]
            new_indents[index] = self._indent_width(line)
            new_depth_before[index] = depth
            depth = self._next_depth(line, depth)
            new_depth_after[index] = depth

        self._lines = new_lines
        self._line_indents = new_indents
        self._depth_before = new_depth_before
        self._depth_after = new_depth_after
        self._folds = tuple(self._build_folds(new_lines, new_indents))

        return ParseSummary(
            changed=True,
            changed_start=changed_start,
            changed_end=changed_end,
            parsed_from=parse_from,
            parsed_lines=max(0, new_len - parse_from),
        )

    def folds(self) -> tuple[FoldRange, ...]:
        return self._folds

    def fold_starting_at(self, line: int) -> FoldRange | None:
        best: FoldRange | None = None
        for fold in self._folds:
            if fold.start_line != line:
                continue
            if best is None or (fold.end_line - fold.start_line) < (best.end_line - best.start_line):
                best = fold
        return best

    def depth_before_line(self, line: int) -> int:
        if not self._depth_before:
            return 0
        if line <= 0:
            return max(0, self._depth_before[0])
        if line < len(self._depth_before):
            return max(0, self._depth_before[line])
        return max(0, self._depth_after[-1] if self._depth_after else 0)

    def enclosing_fold(self, line: int) -> FoldRange | None:
        best: FoldRange | None = None
        for fold in self._folds:
            if fold.start_line <= line <= fold.end_line:
                if best is None or (fold.end_line - fold.start_line) < (best.end_line - best.start_line):
                    best = fold
        return best

    def _indent_width(self, text: str) -> int:
        width = 0
        for char in text:
            if char == " ":
                width += 1
                continue
            if char == "\t":
                width += 4
                continue
            break
        return width

    def _next_depth(self, text: str, current_depth: int) -> int:
        depth = max(0, current_depth)
        in_single = False
        in_double = False
        escaped = False
        for char in text:
            if escaped:
                escaped = False
                continue
            if (in_single or in_double) and char == "\\":
                escaped = True
                continue
            if in_single:
                if char == "'":
                    in_single = False
                continue
            if in_double:
                if char == '"':
                    in_double = False
                continue
            if char == "'":
                in_single = True
                continue
            if char == '"':
                in_double = True
                continue
            if char in _OPEN_TO_CLOSE:
                depth += 1
                continue
            if char in _CLOSE_TO_OPEN:
                depth = max(0, depth - 1)
        return depth

    def _build_folds(self, lines: list[str], indents: list[int]) -> list[FoldRange]:
        folds: list[FoldRange] = []
        seen: set[tuple[int, int, str]] = set()
        line_count = len(lines)
        if line_count <= 1:
            return folds

        for index in range(line_count - 1):
            current = lines[index]
            if not current.strip():
                continue
            base_indent = indents[index]
            probe = index + 1
            while probe < line_count and not lines[probe].strip():
                probe += 1
            if probe >= line_count:
                continue
            if indents[probe] <= base_indent:
                continue
            end = probe
            cursor = probe + 1
            while cursor < line_count:
                text = lines[cursor]
                if not text.strip():
                    end = cursor
                    cursor += 1
                    continue
                if indents[cursor] > base_indent:
                    end = cursor
                    cursor += 1
                    continue
                break
            if end > index:
                key = (index, end, "indent")
                if key not in seen:
                    seen.add(key)
                    folds.append(FoldRange(start_line=index, end_line=end, kind="indent"))

        stack: list[tuple[str, int]] = []
        for line_index, text in enumerate(lines):
            in_single = False
            in_double = False
            escaped = False
            for char in text:
                if escaped:
                    escaped = False
                    continue
                if (in_single or in_double) and char == "\\":
                    escaped = True
                    continue
                if in_single:
                    if char == "'":
                        in_single = False
                    continue
                if in_double:
                    if char == '"':
                        in_double = False
                    continue
                if char == "'":
                    in_single = True
                    continue
                if char == '"':
                    in_double = True
                    continue
                if char in _OPEN_TO_CLOSE:
                    stack.append((char, line_index))
                    continue
                opener = _CLOSE_TO_OPEN.get(char)
                if opener is None or not stack:
                    continue
                if stack[-1][0] != opener:
                    match_index = -1
                    for probe in range(len(stack) - 1, -1, -1):
                        if stack[probe][0] == opener:
                            match_index = probe
                            break
                    if match_index < 0:
                        continue
                    open_char, start_line = stack[match_index]
                    del stack[match_index:]
                else:
                    open_char, start_line = stack.pop()
                if line_index > start_line:
                    key = (start_line, line_index, f"brace:{open_char}")
                    if key in seen:
                        continue
                    seen.add(key)
                    folds.append(FoldRange(start_line=start_line, end_line=line_index, kind=f"brace:{open_char}"))

        folds.sort(key=lambda item: (item.start_line, item.end_line, item.kind))
        return folds
