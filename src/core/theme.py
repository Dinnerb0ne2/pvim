from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .terminal_capabilities import TerminalCapabilities

CSI = "\x1b["
RESET = f"{CSI}0m"

DEFAULT_THEME_SPEC: dict[str, Any] = {
    "ui": {
        "editor": {"fg": "#c0caf5", "bg": "#1a1b26"},
        "cursor_line": {"fg": "#c0caf5", "bg": "#24283b"},
        "line_number": {"fg": "#565f89", "bg": "#1a1b26"},
        "line_number_current": {"fg": "#c0caf5", "bg": "#2f354f", "bold": True},
        "tilde": {"fg": "#414868", "bg": "#1a1b26"},
        "status": {"fg": "#1a1b26", "bg": "#7aa2f7", "bold": True},
        "mode_normal": {"fg": "#1a1b26", "bg": "#7dcfff", "bold": True},
        "mode_insert": {"fg": "#1a1b26", "bg": "#9ece6a", "bold": True},
        "mode_command": {"fg": "#1a1b26", "bg": "#bb9af7", "bold": True},
        "message_info": {"fg": "#9ece6a", "bg": "#16161e"},
        "message_error": {"fg": "#f7768e", "bg": "#16161e", "bold": True},
        "command_line": {"fg": "#c0caf5", "bg": "#1f2335"},
        "sidebar": {"fg": "#a9b1d6", "bg": "#1f2335"},
        "sidebar_header": {"fg": "#1a1b26", "bg": "#73daca", "bold": True},
        "sidebar_current": {"fg": "#1a1b26", "bg": "#e0af68", "bold": True},
        "selection": {"fg": "#c0caf5", "bg": "#33467c"},
        "fuzzy_selected": {"fg": "#1a1b26", "bg": "#9ece6a", "bold": True},
    },
    "syntax": {
        "keyword": {"fg": "#bb9af7"},
        "builtin": {"fg": "#7aa2f7"},
        "function": {"fg": "#73daca"},
        "type": {"fg": "#e0af68"},
        "decorator": {"fg": "#7dcfff"},
        "string": {"fg": "#9ece6a"},
        "comment": {"fg": "#565f89"},
        "number": {"fg": "#ff9e64"},
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


def _rgb_to_ansi_256(r: int, g: int, b: int) -> int:
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + int((r - 8) / 247 * 24)
    return 16 + (36 * round(r / 255 * 5)) + (6 * round(g / 255 * 5)) + round(b / 255 * 5)


ANSI16_RGB: tuple[tuple[int, int, int, int, int], ...] = (
    (0, 0, 0, 30, 40),
    (205, 49, 49, 31, 41),
    (13, 188, 121, 32, 42),
    (229, 229, 16, 33, 43),
    (36, 114, 200, 34, 44),
    (188, 63, 188, 35, 45),
    (17, 168, 205, 36, 46),
    (229, 229, 229, 37, 47),
    (102, 102, 102, 90, 100),
    (241, 76, 76, 91, 101),
    (35, 209, 139, 92, 102),
    (245, 245, 67, 93, 103),
    (59, 142, 234, 94, 104),
    (214, 112, 214, 95, 105),
    (41, 184, 219, 96, 106),
    (255, 255, 255, 97, 107),
)


def _rgb_to_ansi_16(r: int, g: int, b: int, *, background: bool) -> int:
    best_code = 30 if not background else 40
    best_distance: float | None = None
    for pr, pg, pb, fg_code, bg_code in ANSI16_RGB:
        distance = float((r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_code = bg_code if background else fg_code
    return best_code


def _style_from_spec(spec: Mapping[str, Any], capabilities: TerminalCapabilities) -> str:
    parts: list[str] = []
    if bool(spec.get("bold")):
        parts.append(f"{CSI}1m")
    if bool(spec.get("underline")):
        parts.append(f"{CSI}4m")

    fg = spec.get("fg")
    if isinstance(fg, str):
        r, g, b = _hex_to_rgb(fg)
        if capabilities.true_color:
            parts.append(f"{CSI}38;2;{r};{g};{b}m")
        elif capabilities.color_level >= 256:
            parts.append(f"{CSI}38;5;{_rgb_to_ansi_256(r, g, b)}m")
        else:
            parts.append(f"{CSI}{_rgb_to_ansi_16(r, g, b, background=False)}m")

    bg = spec.get("bg")
    if isinstance(bg, str):
        r, g, b = _hex_to_rgb(bg)
        if capabilities.true_color:
            parts.append(f"{CSI}48;2;{r};{g};{b}m")
        elif capabilities.color_level >= 256:
            parts.append(f"{CSI}48;5;{_rgb_to_ansi_256(r, g, b)}m")
        else:
            parts.append(f"{CSI}{_rgb_to_ansi_16(r, g, b, background=True)}m")

    return "".join(parts)


@dataclass(slots=True)
class Theme:
    ui: dict[str, str]
    syntax: dict[str, str]
    capabilities: TerminalCapabilities

    def ui_style(self, key: str) -> str:
        return self.ui.get(key, "")

    def syntax_style(self, key: str) -> str:
        return self.syntax.get(key, "")


def load_theme(path: Path | None, capabilities: TerminalCapabilities | None = None) -> Theme:
    caps = capabilities or TerminalCapabilities(
        true_color=True,
        color_level=24,
        unicode_ui=True,
        hyperlink=True,
        sixel=False,
    )
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
        key: _style_from_spec(spec, caps)
        for key, spec in ui_spec.items()
        if isinstance(spec, Mapping)
    }
    syntax = {
        key: _style_from_spec(spec, caps)
        for key, spec in syntax_spec.items()
        if isinstance(spec, Mapping)
    }
    return Theme(ui=ui, syntax=syntax, capabilities=caps)
