from __future__ import annotations

from itertools import zip_longest


def _count_changes(before: list[str], after: list[str]) -> int:
    return sum(1 for left, right in zip_longest(before, after, fillvalue="") if left != right)


def organize_python_imports(lines: list[str]) -> tuple[list[str], int]:
    if not lines:
        return [""], 0

    index = 0
    if lines and lines[0].startswith("#!"):
        index = 1
    if index < len(lines) and "coding" in lines[index] and lines[index].startswith("#"):
        index += 1

    start = index
    imports: list[str] = []
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            imports.append(stripped)
            index += 1
            continue
        if stripped == "":
            index += 1
            continue
        break

    if not imports:
        return lines, 0

    sorted_imports = sorted(dict.fromkeys(imports), key=lambda item: item.lower())
    prefix = lines[:start]
    suffix = lines[index:]
    new_block = list(sorted_imports)
    if suffix and suffix[0].strip() != "":
        new_block.append("")
    updated = prefix + new_block + suffix
    return updated, _count_changes(lines, updated)


def normalize_code_style(
    lines: list[str],
    *,
    tab_size: int,
    language: str,
    organize_imports_enabled: bool,
) -> tuple[list[str], int]:
    cleaned = [line.expandtabs(tab_size).rstrip() for line in lines]
    if not cleaned:
        cleaned = [""]

    while len(cleaned) > 1 and cleaned[-1] == "" and cleaned[-2] == "":
        cleaned.pop()

    changes = _count_changes(lines, cleaned)

    if language.lower() == "python" and organize_imports_enabled:
        cleaned, import_changes = organize_python_imports(cleaned)
        changes += import_changes

    return cleaned, changes
