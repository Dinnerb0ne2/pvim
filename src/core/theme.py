from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

CSI = "\x1b["
RESET = f"{CSI}0m"

DEFAULT_THEME_SPEC: dict[str, Any] = {
    "ui": {
        "editor": {"fg": "#cdd6f4", "bg": "#1e1e2e"},
        "cursor_line": {"fg": "#cdd6f4", "bg": "#313244"},
        "line_number": {"fg": "#6c7086", "bg": "#1e1e2e"},
        "line_number_current": {"fg": "#f9e2af", "bg": "#313244", "bold": True},
        "tilde": {"fg": "#6c7086", "bg": "#1e1e2e"},
        "status": {"fg": "#1e1e2e", "bg": "#89b4fa"},
        "mode_normal": {"fg": "#1e1e2e", "bg": "#89dceb", "bold": True},
        "mode_insert": {"fg": "#1e1e2e", "bg": "#a6e3a1", "bold": True},
        "mode_command": {"fg": "#1e1e2e", "bg": "#cba6f7", "bold": True},
        "message_info": {"fg": "#a6e3a1", "bg": "#11111b"},
        "message_error": {"fg": "#f38ba8", "bg": "#11111b", "bold": True},
        "command_line": {"fg": "#cdd6f4", "bg": "#181825"},
        "sidebar": {"fg": "#bac2de", "bg": "#181825"},
        "sidebar_header": {"fg": "#1e1e2e", "bg": "#94e2d5", "bold": True},
        "sidebar_current": {"fg": "#1e1e2e", "bg": "#f9e2af", "bold": True},
        "selection": {"fg": "#cdd6f4", "bg": "#45475a"},
        "fuzzy_selected": {"fg": "#1e1e2e", "bg": "#a6e3a1", "bold": True},
    },
    "syntax": {
        "keyword": {"fg": "#cba6f7"},
        "builtin": {"fg": "#89b4fa"},
        "string": {"fg": "#a6e3a1"},
        "comment": {"fg": "#6c7086"},
        "number": {"fg": "#fab387"},
    },
}


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {key: _deep_copy(item) for key, item in base.items()}
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = _deep_copy(value)
    return merged


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    clean = color.strip().lstrip("#")
    if len(clean) != 6:
        raise ValueError(f"Invalid color: {color}")
    return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))


def _style_from_spec(spec: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if bool(spec.get("bold")):
        parts.append(f"{CSI}1m")
    if bool(spec.get("underline")):
        parts.append(f"{CSI}4m")

    fg = spec.get("fg")
    if isinstance(fg, str):
        r, g, b = _hex_to_rgb(fg)
        parts.append(f"{CSI}38;2;{r};{g};{b}m")

    bg = spec.get("bg")
    if isinstance(bg, str):
        r, g, b = _hex_to_rgb(bg)
        parts.append(f"{CSI}48;2;{r};{g};{b}m")

    return "".join(parts)


@dataclass(slots=True)
class Theme:
    ui: dict[str, str]
    syntax: dict[str, str]

    def ui_style(self, key: str) -> str:
        return self.ui.get(key, "")

    def syntax_style(self, key: str) -> str:
        return self.syntax.get(key, "")


def load_theme(path: Path | None) -> Theme:
    merged = _deep_copy(DEFAULT_THEME_SPEC)
    if path is not None and path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Theme root must be an object: {path}")
        merged = _deep_merge(DEFAULT_THEME_SPEC, loaded)

    ui_spec = merged.get("ui", {})
    syntax_spec = merged.get("syntax", {})
    if not isinstance(ui_spec, dict):
        ui_spec = {}
    if not isinstance(syntax_spec, dict):
        syntax_spec = {}

    ui = {
        key: _style_from_spec(spec)
        for key, spec in ui_spec.items()
        if isinstance(spec, Mapping)
    }
    syntax = {
        key: _style_from_spec(spec)
        for key, spec in syntax_spec.items()
        if isinstance(spec, Mapping)
    }
    return Theme(ui=ui, syntax=syntax)
