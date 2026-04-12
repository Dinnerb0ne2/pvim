from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..core.config import AppConfig
from ..core.theme import Theme

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class SyntaxProfile:
    name: str
    keywords: frozenset[str]
    builtins: frozenset[str]
    line_comment: str
    string_delimiters: tuple[str, ...]


PLAIN_PROFILE = SyntaxProfile(
    name="plaintext",
    keywords=frozenset(),
    builtins=frozenset(),
    line_comment="#",
    string_delimiters=('"', "'"),
)


class SyntaxManager:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._enabled = config.feature_enabled("syntax_highlighting")
        self._extension_map_loaded = False
        self._extension_map: dict[str, Path] = {}
        self._profile_cache: dict[Path, SyntaxProfile] = {}
        self._default_profile = PLAIN_PROFILE

    def _load_extension_map(self) -> None:
        if self._extension_map_loaded:
            return

        self._extension_map_loaded = True
        map_path = self._config.syntax_language_map_file()
        if map_path is None or not map_path.exists():
            return

        loaded = json.loads(map_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Language map must be object: {map_path}")

        extension_map: dict[str, Path] = {}
        for ext, file_name in loaded.items():
            if not isinstance(ext, str) or not isinstance(file_name, str):
                continue
            normalized_ext = ext.lower()
            if not normalized_ext.startswith("."):
                normalized_ext = f".{normalized_ext}"
            resolved = self._config.resolve_path(file_name)
            if resolved is not None:
                extension_map[normalized_ext] = resolved

        self._extension_map = extension_map

        default_path = self._config.syntax_default_file()
        if default_path is not None and default_path.exists():
            self._default_profile = self._load_profile(default_path)

    def _load_profile(self, path: Path) -> SyntaxProfile:
        path = path.resolve()
        cached = self._profile_cache.get(path)
        if cached is not None:
            return cached

        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Syntax profile must be object: {path}")

        def _read_set(key: str) -> frozenset[str]:
            value = loaded.get(key, [])
            if not isinstance(value, list):
                return frozenset()
            return frozenset(item for item in value if isinstance(item, str))

        delimiters = loaded.get("string_delimiters", ['"', "'"])
        if not isinstance(delimiters, list):
            delimiters = ['"', "'"]

        profile = SyntaxProfile(
            name=str(loaded.get("name", path.stem)),
            keywords=_read_set("keywords"),
            builtins=_read_set("builtins"),
            line_comment=str(loaded.get("line_comment", "#")),
            string_delimiters=tuple(item for item in delimiters if isinstance(item, str)),
        )
        self._profile_cache[path] = profile
        return profile

    def profile_for_file(self, file_path: Path | None) -> SyntaxProfile:
        if not self._enabled:
            return PLAIN_PROFILE

        self._load_extension_map()
        if file_path is None:
            return self._default_profile

        extension = file_path.suffix.lower()
        profile_path = self._extension_map.get(extension)
        if profile_path is None:
            return self._default_profile
        if not profile_path.exists():
            return self._default_profile
        return self._load_profile(profile_path)

    def line_comment_for_file(self, file_path: Path | None) -> str:
        profile = self.profile_for_file(file_path)
        return profile.line_comment or "#"

    def highlight_line(self, text: str, profile: SyntaxProfile, theme: Theme, base_style: str) -> str:
        if not text:
            return base_style

        comment_index = self._find_comment_index(text, profile)
        if comment_index < 0:
            code_part = text
            comment_part = ""
        else:
            code_part = text[:comment_index]
            comment_part = text[comment_index:]

        code = self._highlight_code(code_part, profile, theme, base_style)
        if not comment_part:
            return f"{base_style}{code}"

        comment_style = theme.syntax_style("comment")
        if not comment_style:
            return f"{base_style}{code}{comment_part}"
        return f"{base_style}{code}{comment_style}{comment_part}{base_style}"

    def _highlight_code(self, code: str, profile: SyntaxProfile, theme: Theme, base_style: str) -> str:
        if not code:
            return ""

        spans = self._find_string_spans(code, profile.string_delimiters)
        if not spans:
            return self._highlight_plain_segment(code, profile, theme, base_style)

        out: list[str] = []
        cursor = 0
        string_style = theme.syntax_style("string")
        for start, end in spans:
            if start > cursor:
                out.append(self._highlight_plain_segment(code[cursor:start], profile, theme, base_style))
            chunk = code[start:end]
            if string_style:
                out.append(f"{string_style}{chunk}{base_style}")
            else:
                out.append(chunk)
            cursor = end

        if cursor < len(code):
            out.append(self._highlight_plain_segment(code[cursor:], profile, theme, base_style))
        return "".join(out)

    def _highlight_plain_segment(
        self,
        segment: str,
        profile: SyntaxProfile,
        theme: Theme,
        base_style: str,
    ) -> str:
        out: list[str] = []
        cursor = 0
        previous_token = ""
        for match in TOKEN_RE.finditer(segment):
            start, end = match.span()
            if start > cursor:
                out.append(segment[cursor:start])
            token = match.group(0)
            style = ""
            if token[:1].isdigit():
                style = theme.syntax_style("number")
            elif token in profile.keywords:
                style = theme.syntax_style("keyword")
            elif token in profile.builtins:
                style = theme.syntax_style("builtin")
            elif previous_token == "def":
                style = theme.syntax_style("function")
            elif previous_token == "class":
                style = theme.syntax_style("type")
            else:
                probe = end
                while probe < len(segment) and segment[probe].isspace():
                    probe += 1
                if probe < len(segment) and segment[probe] == "(":
                    style = theme.syntax_style("function")

            if style:
                out.append(f"{style}{token}{base_style}")
            else:
                out.append(token)
            cursor = end
            previous_token = token

        if cursor < len(segment):
            out.append(segment[cursor:])
        return "".join(out)

    def _find_comment_index(self, text: str, profile: SyntaxProfile) -> int:
        prefix = profile.line_comment
        if not prefix:
            return -1

        in_string: str | None = None
        escaped = False
        i = 0
        while i < len(text):
            if in_string:
                if escaped:
                    escaped = False
                elif text[i] == "\\":
                    escaped = True
                elif text[i] == in_string:
                    in_string = None
                i += 1
                continue

            if text.startswith(prefix, i):
                return i
            if text[i] in profile.string_delimiters:
                in_string = text[i]
            i += 1

        return -1

    def _find_string_spans(self, text: str, delimiters: tuple[str, ...]) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        i = 0
        while i < len(text):
            char = text[i]
            if char not in delimiters:
                i += 1
                continue

            quote = char
            start = i
            i += 1
            escaped = False
            while i < len(text):
                if escaped:
                    escaped = False
                    i += 1
                    continue
                if text[i] == "\\":
                    escaped = True
                    i += 1
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            spans.append((start, i))
        return spans
