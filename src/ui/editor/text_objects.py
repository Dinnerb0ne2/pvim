from __future__ import annotations


def is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def word_range(line: str, cursor_col: int, scope: str) -> tuple[int, int] | None:
    if not line:
        return None
    if cursor_col >= len(line):
        index = len(line) - 1
    else:
        index = cursor_col
    if index < 0:
        return None
    if not is_word_char(line[index]) and index > 0 and is_word_char(line[index - 1]):
        index -= 1
    if not is_word_char(line[index]):
        right = index
        while right < len(line) and not is_word_char(line[right]):
            right += 1
        if right >= len(line):
            return None
        index = right

    start = index
    end = index
    while start > 0 and is_word_char(line[start - 1]):
        start -= 1
    while end < len(line) and is_word_char(line[end]):
        end += 1

    if scope == "a":
        while start > 0 and line[start - 1].isspace():
            start -= 1
        while end < len(line) and line[end].isspace():
            end += 1
    return start, end


def quote_range(line: str, cursor_col: int, quote: str, scope: str) -> tuple[int, int] | None:
    left = line.rfind(quote, 0, min(cursor_col + 1, len(line)))
    right = line.find(quote, min(cursor_col + 1, len(line)))
    if left < 0 or right < 0 or left >= right:
        return None
    if scope == "i":
        return left + 1, right
    return left, right + 1
