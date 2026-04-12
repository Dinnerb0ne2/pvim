from __future__ import annotations

import re

WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def word_at_cursor(line: str, column: int) -> str:
    if not line:
        return ""
    cursor = max(0, min(column, len(line)))
    for match in WORD_RE.finditer(line):
        start, end = match.span()
        if start <= cursor < end:
            return match.group(0)
        if cursor > 0 and start <= cursor - 1 < end:
            return match.group(0)
    return ""


def find_next(lines: list[str], query: str, start_row: int, start_col: int) -> tuple[int, int] | None:
    if not query:
        return None
    if not lines:
        return None

    total = len(lines)
    row = max(0, min(start_row, total - 1))
    col = max(0, start_col)

    for current in range(row, total):
        text = lines[current]
        from_col = col if current == row else 0
        index = text.find(query, from_col)
        if index >= 0:
            return current, index

    for current in range(0, row):
        index = lines[current].find(query)
        if index >= 0:
            return current, index

    return None


def replace_next(
    lines: list[str],
    old: str,
    new: str,
    start_row: int,
    start_col: int,
) -> tuple[list[str], tuple[int, int] | None, bool]:
    position = find_next(lines, old, start_row, start_col)
    if position is None:
        return lines, None, False

    row, col = position
    line = lines[row]
    replaced_line = f"{line[:col]}{new}{line[col + len(old):]}"
    updated = list(lines)
    updated[row] = replaced_line
    return updated, (row, col + len(new)), True


def replace_all(lines: list[str], old: str, new: str) -> tuple[list[str], int]:
    if not old:
        return lines, 0
    count = 0
    updated: list[str] = []
    for line in lines:
        replaced, changed = line.replace(old, new), line.count(old)
        updated.append(replaced)
        count += changed
    return updated, count


def rename_symbol(lines: list[str], old: str, new: str) -> tuple[list[str], int]:
    if not old:
        return lines, 0
    pattern = re.compile(rf"\b{re.escape(old)}\b")
    total = 0
    updated: list[str] = []
    for line in lines:
        replaced, count = pattern.subn(new, line)
        updated.append(replaced)
        total += count
    return updated, total
