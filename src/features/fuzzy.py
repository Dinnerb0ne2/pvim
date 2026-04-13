from __future__ import annotations

from pathlib import Path
from typing import Iterable


def fuzzy_score(candidate: str, query: str) -> float | None:
    query = query.strip().lower()
    if not query:
        return 0.0

    text = candidate.lower()
    index = -1
    score = 0.0
    streak = 0.0
    for token_index, token in enumerate(query):
        found = text.find(token, index + 1)
        if found < 0:
            return None

        if token_index == 0:
            score -= found * 1.5

        if index >= 0:
            gap = found - index - 1
            score -= gap * 0.2
            if gap == 0:
                streak += 0.8
            else:
                streak = 0.0

        if found == 0 or text[found - 1] in "\\/_-. ":
            score += 3.0

        score += 4.0
        index = found

    score += streak
    score -= len(text) * 0.01
    return score


def fuzzy_filter(candidates: Iterable[Path], query: str, *, limit: int = 40) -> list[Path]:
    query = query.strip()
    if not query:
        return list(candidates)[:limit]

    scored: list[tuple[float, Path]] = []
    for candidate in candidates:
        score = fuzzy_score(str(candidate), query)
        if score is None:
            continue
        scored.append((score, candidate))

    scored.sort(key=lambda item: (-item[0], len(str(item[1])), str(item[1]).lower()))
    return [item[1] for item in scored[:limit]]
