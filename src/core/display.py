from __future__ import annotations

import unicodedata


def char_display_width(char: str) -> int:
    if not char:
        return 0
    category = unicodedata.category(char)
    if category.startswith("C"):
        return 0
    if unicodedata.combining(char):
        return 0
    east_asian = unicodedata.east_asian_width(char)
    if east_asian in {"W", "F"}:
        return 2
    return 1


def display_width(text: str) -> int:
    return sum(char_display_width(char) for char in text)


def index_from_display_col(text: str, target_col: int) -> int:
    if target_col <= 0:
        return 0
    width = 0
    for index, char in enumerate(text):
        next_width = width + char_display_width(char)
        if next_width > target_col:
            return index
        width = next_width
    return len(text)


def slice_by_display(text: str, start_col: int, max_width: int) -> str:
    if max_width <= 0:
        return ""
    start_index = index_from_display_col(text, max(0, start_col))
    out: list[str] = []
    width = 0
    for char in text[start_index:]:
        char_width = char_display_width(char)
        if width + char_width > max_width:
            break
        out.append(char)
        width += char_width
    return "".join(out)


def pad_to_display(text: str, width: int) -> str:
    current = display_width(text)
    if current >= width:
        return text
    return text + (" " * (width - current))
