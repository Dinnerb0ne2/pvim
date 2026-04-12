from __future__ import annotations

import re
from typing import Iterable

WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class TabCompletionFeature:
    __slots__ = ("enabled", "visible", "items", "selected", "scroll", "title")

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.visible = False
        self.items: list[tuple[str, list[int]]] = []
        self.selected = 0
        self.scroll = 0
        self.title = "COMPLETION"

    def open(
        self,
        query: str,
        lines: Iterable[str],
        ast_hint: str = "",
        extra_candidates: Iterable[str] = (),
    ) -> None:
        if not self.enabled:
            return
        candidates = self._collect_candidates(lines, ast_hint, extra_candidates)
        self.items = self._filter(query, candidates, limit=30)
        self.selected = 0
        self.scroll = 0
        self.visible = bool(self.items)

    def close(self) -> None:
        self.visible = False
        self.items = []

    def move_up(self) -> None:
        if self.selected > 0:
            self.selected -= 1
        if self.selected < self.scroll:
            self.scroll = self.selected

    def move_down(self, visible_rows: int) -> None:
        if self.selected < len(self.items) - 1:
            self.selected += 1
        if self.selected >= self.scroll + max(1, visible_rows):
            self.scroll = self.selected - visible_rows + 1

    def selected_text(self) -> str | None:
        if not self.items:
            return None
        return self.items[self.selected][0]

    def visible_items(self, height: int) -> list[tuple[str, list[int]]]:
        rows = max(1, height)
        end = min(len(self.items), self.scroll + rows)
        return self.items[self.scroll : end]

    def _collect_candidates(
        self,
        lines: Iterable[str],
        ast_hint: str,
        extra_candidates: Iterable[str],
    ) -> list[str]:
        bucket: set[str] = set()
        for line in lines:
            for match in WORD_RE.findall(line):
                if len(match) >= 2:
                    bucket.add(match)
        for item in WORD_RE.findall(ast_hint):
            if len(item) >= 2:
                bucket.add(item)
        for item in extra_candidates:
            clean = str(item).strip()
            if len(clean) >= 2:
                bucket.add(clean)
        return sorted(bucket, key=lambda item: (len(item), item.lower()))

    def _filter(self, query: str, candidates: list[str], *, limit: int) -> list[tuple[str, list[int]]]:
        clean = query.strip()
        if not clean:
            return [(item, []) for item in candidates[:limit]]
        matched: list[tuple[int, int, str, list[int]]] = []
        for candidate in candidates:
            indices = self._greedy_subsequence(clean, candidate)
            if indices is None:
                continue
            gap_score = indices[-1] - indices[0] if indices else 0
            matched.append((gap_score, len(candidate), candidate, indices))
        matched.sort(key=lambda item: (item[0], item[1], item[2].lower()))
        return [(item[2], item[3]) for item in matched[:limit]]

    def _greedy_subsequence(self, query: str, candidate: str) -> list[int] | None:
        indices: list[int] = []
        start = 0
        lower_candidate = candidate.lower()
        lower_query = query.lower()
        for char in lower_query:
            found = lower_candidate.find(char, start)
            if found < 0:
                return None
            indices.append(found)
            start = found + 1
        return indices
