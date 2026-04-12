from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG: dict[str, Any] = {
    "python": {
        "required": "3.14.3",
    },
    "editor": {
        "line_numbers": True,
        "tab_size": 4,
    },
    "theme": {
        "enabled": True,
        "config_file": "pvim.theme.default.json",
    },
    "performance": {
        "experimental_jit": True,
        "lazy_load": True,
        "profile_top_n": 25,
    },
    "features": {
        "syntax_highlighting": {
            "enabled": True,
            "language_map_file": "syntax\\languages.json",
            "default_file": "syntax\\plaintext.json",
        },
        "auto_pairs": {
            "enabled": True,
            "config_file": "autopairs.json",
        },
        "sidebar": {
            "enabled": False,
            "width": 30,
            "max_files": 3000,
        },
        "vscode_shortcuts": {
            "enabled": True,
            "bindings": {
                "toggle_comment": "CTRL_SLASH",
                "add_cursor_down": "CTRL_D",
                "clear_multi_cursor": "CTRL_U",
                "word_left": "CTRL_LEFT",
                "word_right": "CTRL_RIGHT",
                "quick_find": "CTRL_F",
                "quick_replace": "CTRL_G",
                "open_completion": "CTRL_N",
                "fuzzy_finder": "CTRL_P",
                "toggle_file_tree": "F3",
                "toggle_sidebar": "F4",
                "format_code": "F8",
                "refactor_rename": "CTRL_R",
            },
        },
        "key_hints": {
            "enabled": False,
            "trigger": "F1",
        },
        "fuzzy_finder": {
            "enabled": False,
        },
        "scripting": {
            "enabled": False,
            "step_limit": 100000,
        },
        "plugins": {
            "enabled": False,
            "directory": "plugins",
            "auto_load": True,
        },
        "plugin_keyhooks": {
            "enabled": False,
        },
        "tabline": {
            "enabled": False,
        },
        "winbar": {
            "enabled": False,
        },
        "file_tree": {
            "enabled": False,
        },
        "tab_completion": {
            "enabled": False,
        },
        "git_control": {
            "enabled": False,
        },
        "notifications": {
            "enabled": False,
        },
        "git_status": {
            "enabled": False,
            "refresh_seconds": 2.0,
        },
        "refactor_tools": {
            "enabled": False,
        },
        "find_replace": {
            "enabled": True,
        },
        "code_style_normalizer": {
            "enabled": False,
        },
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


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _as_int(value: Any, *, default: int, minimum: int = 0) -> int:
    if isinstance(value, int):
        return max(minimum, value)
    return max(minimum, default)


def _as_float(value: Any, *, default: float, minimum: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return max(minimum, float(value))
    return max(minimum, default)


@dataclass(slots=True)
class AppConfig:
    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        config_path = path or (Path.cwd() / "pvim.config.json")
        merged = _deep_copy(DEFAULT_CONFIG)

        if config_path.exists():
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"Config root must be an object: {config_path}")
            merged = _deep_merge(DEFAULT_CONFIG, loaded)

        return cls(path=config_path.resolve(), data=merged)

    def _lookup(self, *keys: str, default: Any = None) -> Any:
        current: Any = self.data
        for key in keys:
            if not isinstance(current, Mapping) or key not in current:
                return default
            current = current[key]
        return current

    def resolve_path(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if path.is_absolute():
            return path
        return (self.path.parent / path).resolve()

    def required_python(self) -> str:
        value = self._lookup("python", "required", default="3.14.3")
        return value if isinstance(value, str) else "3.14.3"

    def show_line_numbers(self) -> bool:
        return _as_bool(self._lookup("editor", "line_numbers", default=True), default=True)

    def tab_size(self) -> int:
        return _as_int(self._lookup("editor", "tab_size", default=4), default=4, minimum=1)

    def theme_enabled(self) -> bool:
        return _as_bool(self._lookup("theme", "enabled", default=True), default=True)

    def theme_file(self) -> Path | None:
        file_name = self._lookup("theme", "config_file", default="pvim.theme.default.json")
        return self.resolve_path(file_name if isinstance(file_name, str) else None)

    def feature_enabled(self, name: str) -> bool:
        return _as_bool(self._lookup("features", name, "enabled", default=True), default=True)

    def syntax_language_map_file(self) -> Path | None:
        file_name = self._lookup(
            "features",
            "syntax_highlighting",
            "language_map_file",
            default="syntax\\languages.json",
        )
        return self.resolve_path(file_name if isinstance(file_name, str) else None)

    def syntax_default_file(self) -> Path | None:
        file_name = self._lookup(
            "features",
            "syntax_highlighting",
            "default_file",
            default="syntax\\plaintext.json",
        )
        return self.resolve_path(file_name if isinstance(file_name, str) else None)

    def auto_pairs_file(self) -> Path | None:
        file_name = self._lookup(
            "features",
            "auto_pairs",
            "config_file",
            default="autopairs.json",
        )
        return self.resolve_path(file_name if isinstance(file_name, str) else None)

    def sidebar_enabled(self) -> bool:
        return self.feature_enabled("sidebar")

    def sidebar_width(self) -> int:
        return _as_int(
            self._lookup("features", "sidebar", "width", default=30),
            default=30,
            minimum=16,
        )

    def file_scan_limit(self) -> int:
        return _as_int(
            self._lookup("features", "sidebar", "max_files", default=3000),
            default=3000,
            minimum=100,
        )

    def git_refresh_seconds(self) -> float:
        return _as_float(
            self._lookup("features", "git_status", "refresh_seconds", default=2.0),
            default=2.0,
            minimum=0.2,
        )

    def shortcuts_enabled(self) -> bool:
        return _as_bool(
            self._lookup("features", "vscode_shortcuts", "enabled", default=True),
            default=True,
        )

    def shortcut(self, action: str, default: str) -> str:
        value = self._lookup("features", "vscode_shortcuts", "bindings", action, default=default)
        return value if isinstance(value, str) else default

    def key_hints_enabled(self) -> bool:
        return _as_bool(self._lookup("features", "key_hints", "enabled", default=True), default=True)

    def key_hints_trigger(self) -> str:
        value = self._lookup("features", "key_hints", "trigger", default="F1")
        return value if isinstance(value, str) else "F1"

    def script_step_limit(self) -> int:
        return _as_int(
            self._lookup("features", "scripting", "step_limit", default=100000),
            default=100000,
            minimum=1000,
        )

    def plugins_directory(self) -> Path:
        value = self._lookup("features", "plugins", "directory", default="plugins")
        if not isinstance(value, str):
            value = "plugins"
        resolved = self.resolve_path(value)
        return resolved if resolved is not None else (self.path.parent / "plugins").resolve()

    def plugins_auto_load(self) -> bool:
        return _as_bool(self._lookup("features", "plugins", "auto_load", default=True), default=True)

    def experimental_jit_enabled(self) -> bool:
        return _as_bool(self._lookup("performance", "experimental_jit", default=True), default=True)

    def lazy_load_enabled(self) -> bool:
        return _as_bool(self._lookup("performance", "lazy_load", default=True), default=True)

    def profile_top_n(self) -> int:
        return _as_int(self._lookup("performance", "profile_top_n", default=25), default=25, minimum=5)
