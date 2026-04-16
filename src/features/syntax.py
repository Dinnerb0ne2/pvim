from __future__ import annotations

import builtins
from collections import defaultdict
import io
import json
import keyword
import re
import token as token_types
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..core.config import AppConfig
from ..core.theme import Theme

TOKEN_RE = re.compile(
    r"@[A-Za-z_][A-Za-z0-9_]*"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|[A-Za-z_][A-Za-z0-9_:-]*!?"
    r"|\d+(?:\.\d+)?"
    r"|==|!=|<=|>=|=>|->|\+\+|--|\+=|-=|\*=|/=|%=|&&|\|\||::|\?\?"
    r"|[+\-*/%=&|^~<>!?]"
    r"|[(){}\[\],.;:]"
)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
OPERATOR_TOKENS = frozenset(
    {
        "==",
        "!=",
        "<=",
        ">=",
        "=>",
        "->",
        "++",
        "--",
        "+=",
        "-=",
        "*=",
        "/=",
        "%=",
        "&&",
        "||",
        "::",
        "??",
        "+",
        "-",
        "*",
        "/",
        "%",
        "=",
        "&",
        "|",
        "^",
        "~",
        "<",
        ">",
        "!",
        "?",
    }
)
PUNCTUATION_TOKENS = frozenset({"(", ")", "{", "}", "[", "]", ",", ".", ";", ":"})
CONSTANT_TOKENS = frozenset({"true", "false", "null", "none", "undefined", "nan", "inf", "infinity"})


@dataclass(frozen=True, slots=True)
class SyntaxProfile:
    name: str
    keywords: frozenset[str]
    builtins: frozenset[str]
    line_comment: str
    string_delimiters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegexTokenRule:
    pattern: re.Pattern[str]
    style: str


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
        self._line_cache: defaultdict[str, dict[str, str]] = defaultdict(dict)
        self._regex_rules_loaded = False
        self._regex_rules: dict[str, tuple[RegexTokenRule, ...]] = {}
        self._python_builtins = frozenset(name for name in dir(builtins) if isinstance(name, str))

    def reload(self) -> None:
        self._enabled = self._config.feature_enabled("syntax_highlighting")
        self._extension_map_loaded = False
        self._extension_map = {}
        self._profile_cache = {}
        self._default_profile = PLAIN_PROFILE
        self._line_cache = defaultdict(dict)
        self._regex_rules_loaded = False
        self._regex_rules = {}

    def _load_extension_map(self) -> None:
        if self._extension_map_loaded:
            return

        self._extension_map_loaded = True
        extension_map: dict[str, Path] = {}
        for map_path in self._config.syntax_language_map_files():
            if not map_path.exists():
                continue
            loaded = json.loads(map_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"Language map must be object: {map_path}")
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

    def _load_regex_rules(self) -> None:
        if self._regex_rules_loaded:
            return
        self._regex_rules_loaded = True
        loaded_rules: dict[str, tuple[RegexTokenRule, ...]] = {}
        path = self._config.syntax_regex_rules_file()
        if path is None or not path.exists():
            self._regex_rules = loaded_rules
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            self._regex_rules = loaded_rules
            return
        for language, rules in payload.items():
            if not isinstance(language, str) or not isinstance(rules, list):
                continue
            compiled: list[RegexTokenRule] = []
            for item in rules:
                if not isinstance(item, dict):
                    continue
                pattern = item.get("pattern")
                style = item.get("style")
                if not isinstance(pattern, str) or not isinstance(style, str):
                    continue
                try:
                    regex = re.compile(pattern)
                except re.error:
                    continue
                compiled.append(RegexTokenRule(pattern=regex, style=style.strip()))
            if compiled:
                loaded_rules[language.strip().lower()] = tuple(compiled)
        self._regex_rules = loaded_rules

    def _token_style_from_regex_rules(self, profile: SyntaxProfile, token: str) -> str:
        if not token:
            return ""
        self._load_regex_rules()
        rules = self._regex_rules.get(profile.name.strip().lower(), ())
        for rule in rules:
            if rule.pattern.fullmatch(token):
                return rule.style
        return ""

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
        cache_key = f"{base_style}\x00{text}"
        bucket = self._line_cache[profile.name]
        cached = bucket.get(cache_key)
        if cached is not None:
            return cached

        if not text:
            rendered = base_style
        elif profile.name.strip().lower() == "python":
            rendered = self._highlight_python_line(text, profile, theme, base_style)
        else:
            comment_index = self._find_comment_index(text, profile)
            if comment_index < 0:
                code_part = text
                comment_part = ""
            else:
                code_part = text[:comment_index]
                comment_part = text[comment_index:]

            code = self._highlight_code(code_part, profile, theme, base_style)
            if not comment_part:
                rendered = f"{base_style}{code}"
            else:
                comment_style = theme.syntax_style("comment")
                if not comment_style:
                    rendered = f"{base_style}{code}{comment_part}"
                else:
                    rendered = f"{base_style}{code}{comment_style}{comment_part}{base_style}"
        if len(bucket) > 6000:
            bucket.clear()
        bucket[cache_key] = rendered
        return rendered

    def _highlight_python_line(self, text: str, profile: SyntaxProfile, theme: Theme, base_style: str) -> str:
        out: list[str] = [base_style]
        cursor = 0
        text_length = len(text)
        previous_keyword = ""
        decorator_next = False
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for token_info in tokens:
                token_type = token_info.type
                token_text = token_info.string
                start_col = min(text_length, max(0, token_info.start[1]))
                end_col = min(text_length, max(start_col, token_info.end[1]))
                segment_start = max(cursor, start_col)
                segment_end = max(segment_start, end_col)
                if segment_start > cursor:
                    out.append(text[cursor:segment_start])
                segment = text[segment_start:segment_end]
                if not segment:
                    cursor = max(cursor, segment_end)
                    continue

                style = ""
                if token_type == token_types.COMMENT:
                    style = theme.syntax_style("comment")
                elif token_type == token_types.STRING:
                    style = theme.syntax_style("string")
                elif token_type == token_types.NUMBER:
                    style = theme.syntax_style("number")
                elif token_type == token_types.NAME:
                    if decorator_next:
                        style = theme.syntax_style("decorator")
                        decorator_next = False
                    elif token_text in {"True", "False", "None"}:
                        style = theme.syntax_style("constant")
                    elif keyword.iskeyword(token_text):
                        style = theme.syntax_style("keyword")
                    elif token_text in profile.builtins or token_text in self._python_builtins:
                        style = theme.syntax_style("builtin")
                    elif previous_keyword == "def":
                        style = theme.syntax_style("function")
                    elif previous_keyword == "class":
                        style = theme.syntax_style("type")
                elif token_type == token_types.OP and token_text == "@":
                    style = theme.syntax_style("decorator")
                    decorator_next = True
                elif token_type == token_types.OP:
                    if token_text in PUNCTUATION_TOKENS:
                        style = theme.syntax_style("punctuation")
                    elif token_text in OPERATOR_TOKENS:
                        style = theme.syntax_style("operator")

                if style:
                    out.append(f"{style}{segment}{base_style}")
                else:
                    out.append(segment)
                cursor = max(cursor, segment_end)

                if token_type == token_types.NAME and token_text in {"def", "class"}:
                    previous_keyword = token_text
                elif token_type not in {
                    token_types.INDENT,
                    token_types.DEDENT,
                    token_types.NEWLINE,
                    tokenize.NL,
                }:
                    previous_keyword = ""
        except (tokenize.TokenError, IndentationError):
            return f"{base_style}{self._highlight_code(text, profile, theme, base_style)}"

        if cursor < len(text):
            out.append(text[cursor:])
        rendered = "".join(out)
        if ANSI_ESCAPE_RE.sub("", rendered) != text:
            # Guard against token edge-cases that may duplicate characters while typing.
            return f"{base_style}{self._highlight_code(text, profile, theme, base_style)}"
        return rendered

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
            elif token in OPERATOR_TOKENS:
                style = theme.syntax_style("operator")
            elif token in PUNCTUATION_TOKENS:
                style = theme.syntax_style("punctuation")
            elif token.lower() in CONSTANT_TOKENS:
                style = theme.syntax_style("constant")
            elif token.startswith("@"):
                style = theme.syntax_style("decorator")
            elif token in profile.keywords:
                style = theme.syntax_style("keyword")
            elif token in profile.builtins:
                style = theme.syntax_style("builtin")
            else:
                regex_style = self._token_style_from_regex_rules(profile, token)
                if regex_style:
                    style = theme.syntax_style(regex_style)
            if not style and previous_token == "def":
                style = theme.syntax_style("function")
            elif not style and previous_token == "class":
                style = theme.syntax_style("type")
            elif not style:
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
            if token in OPERATOR_TOKENS or token in PUNCTUATION_TOKENS:
                previous_token = ""
            else:
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
