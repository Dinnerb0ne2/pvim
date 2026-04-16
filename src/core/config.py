from __future__ import annotations

import json
import getpass
import shlex
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any, Mapping

DEFAULT_CONFIG: dict[str, Any] = {
    "python": {
        "required": "3.14.3",
    },
    "runtime": {
        "directory": "",
    },
    "editor": {
        "line_numbers": True,
        "tab_size": 4,
        "soft_wrap": True,
        "default_line_ending": "lf",
        "preserve_line_ending": True,
        "encodings": ["utf-8", "utf-8-sig", "gb18030", "gbk", "big5", "shift_jis", "latin-1"],
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
            "extra_language_map_files": [],
            "default_file": "syntax\\plaintext.json",
            "regex_rules_file": "syntax\\regex_rules.json",
        },
        "auto_pairs": {
            "enabled": True,
            "config_file": "autopairs.json",
        },
        "sidebar": {
            "enabled": True,
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
                "jump_back": "CTRL_O",
                "toggle_file_tree": "F3",
                "toggle_sidebar": "F4",
                "format_code": "F8",
                "refactor_rename": "F6",
            },
            "filetype_bindings": {},
        },
        "key_hints": {
            "enabled": True,
            "trigger": "F1",
        },
        "fuzzy_finder": {
            "enabled": True,
        },
        "live_grep": {
            "enabled": True,
            "max_results": 200,
        },
        "lsp": {
            "enabled": False,
            "command": [],
            "timeout_seconds": 1.2,
            "language_id_map": {
                ".py": "python",
                ".pyi": "python",
                ".pyw": "python",
                ".js": "javascript",
                ".jsx": "javascriptreact",
                ".ts": "typescript",
                ".tsx": "typescriptreact",
                ".json": "json",
                ".jsonc": "jsonc",
                ".go": "go",
                ".rs": "rust",
                ".java": "java",
                ".c": "c",
                ".h": "c",
                ".cc": "cpp",
                ".cpp": "cpp",
                ".cxx": "cpp",
                ".hpp": "cpp",
                ".cs": "csharp",
                ".kt": "kotlin",
                ".kts": "kotlin",
                ".lua": "lua",
                ".toml": "toml",
                ".sql": "sql",
                ".md": "markdown",
                ".html": "html",
                ".css": "css",
                ".scss": "scss",
                ".less": "less",
                ".yaml": "yaml",
                ".yml": "yaml",
                ".sh": "shellscript",
                ".bash": "shellscript",
                ".zsh": "shellscript",
                ".ps1": "powershell"
            },
        },
        "text_objects": {
            "enabled": True,
        },
        "undo_tree": {
            "enabled": True,
            "max_actions": 400,
        },
        "macros": {
            "enabled": True,
        },
        "scripting": {
            "enabled": False,
            "step_limit": 1000000,
        },
        "piece_table": {
            "enabled": True,
            "large_file_line_threshold": 50000,
        },
        "swap": {
            "enabled": True,
            "interval_seconds": 4.0,
            "directory": "",
        },
        "auto_save": {
            "enabled": True,
            "interval_seconds": 8.0,
        },
        "session": {
            "enabled": True,
            "restore_on_startup": False,
            "file": "",
            "profiles_directory": "",
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
            "enabled": True,
        },
        "winbar": {
            "enabled": True,
        },
        "file_tree": {
            "enabled": True,
            "sort_by": "name",
            "filter_query": "",
            "show_hidden": False,
        },
        "tab_completion": {
            "enabled": True,
        },
        "git_control": {
            "enabled": True,
        },
        "notifications": {
            "enabled": True,
        },
        "git_status": {
            "enabled": True,
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
        "config_reload": {
            "enabled": True,
            "interval_seconds": 1.0,
        },
        "quickfix": {
            "enabled": True,
            "max_items": 300,
        },
        "autocmds": {
            "enabled": True,
            "events": {},
            "filetypes": {},
        },
        "scoped_vars": {
            "enabled": True,
        },
        "clipboard": {
            "enabled": True,
        },
        "dap": {
            "enabled": True,
            "python_command": "python",
        },
        "plugin_sandbox": {
            "enabled": True,
            "allow_actions": [
                "message",
                "line_count",
                "get_line",
                "set_line",
                "cursor",
                "find",
                "replace_all",
                "current_file",
                "virtual.add",
                "virtual.set",
                "virtual.clear",
                "virtual.get",
                "ast.node_at",
            ],
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

    def runtime_directory(self) -> Path:
        value = self._lookup("runtime", "directory", default="")
        if isinstance(value, str) and value.strip():
            path = Path(value.strip()).expanduser()
            if path.is_absolute():
                return path.resolve()
            return (self.path.parent / path).resolve()
        user = "".join(ch for ch in getpass.getuser().strip() if ch.isalnum() or ch in {"_", "-"})
        if not user:
            user = "user"
        return (Path(tempfile.gettempdir()) / "pvim-runtime" / user).resolve()

    def show_line_numbers(self) -> bool:
        return _as_bool(self._lookup("editor", "line_numbers", default=True), default=True)

    def tab_size(self) -> int:
        return _as_int(self._lookup("editor", "tab_size", default=4), default=4, minimum=1)

    def soft_wrap_enabled(self) -> bool:
        return _as_bool(self._lookup("editor", "soft_wrap", default=True), default=True)

    def preserve_line_ending(self) -> bool:
        return _as_bool(self._lookup("editor", "preserve_line_ending", default=True), default=True)

    def default_line_ending(self) -> str:
        value = self._lookup("editor", "default_line_ending", default="lf")
        if not isinstance(value, str):
            return "\n"
        lowered = value.strip().lower()
        if lowered in {"crlf", "windows"}:
            return "\r\n"
        return "\n"

    def preferred_encodings(self) -> list[str]:
        value = self._lookup("editor", "encodings", default=["utf-8"])
        if not isinstance(value, list):
            return ["utf-8"]
        parsed: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            clean = item.strip().lower()
            if clean and clean not in parsed:
                parsed.append(clean)
        return parsed or ["utf-8"]

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

    def syntax_language_map_files(self) -> list[Path]:
        files: list[Path] = []
        primary = self.syntax_language_map_file()
        if primary is not None:
            files.append(primary)
        extra = self._lookup("features", "syntax_highlighting", "extra_language_map_files", default=[])
        if isinstance(extra, list):
            for item in extra:
                if not isinstance(item, str):
                    continue
                resolved = self.resolve_path(item)
                if resolved is None:
                    continue
                if resolved not in files:
                    files.append(resolved)
        return files

    def syntax_default_file(self) -> Path | None:
        file_name = self._lookup(
            "features",
            "syntax_highlighting",
            "default_file",
            default="syntax\\plaintext.json",
        )
        return self.resolve_path(file_name if isinstance(file_name, str) else None)

    def syntax_regex_rules_file(self) -> Path | None:
        file_name = self._lookup(
            "features",
            "syntax_highlighting",
            "regex_rules_file",
            default="syntax\\regex_rules.json",
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

    def shortcut_for_language(self, action: str, default: str, language_id: str | None) -> str:
        fallback = self.shortcut(action, default)
        if not language_id:
            return fallback
        language = language_id.strip().lower()
        if not language:
            return fallback
        value = self._lookup(
            "features",
            "vscode_shortcuts",
            "filetype_bindings",
            language,
            action,
            default=None,
        )
        if isinstance(value, str) and value.strip():
            return value.strip()
        return fallback

    def key_hints_enabled(self) -> bool:
        return _as_bool(self._lookup("features", "key_hints", "enabled", default=True), default=True)

    def key_hints_trigger(self) -> str:
        value = self._lookup("features", "key_hints", "trigger", default="F1")
        return value if isinstance(value, str) else "F1"

    def script_step_limit(self) -> int:
        return _as_int(
            self._lookup("features", "scripting", "step_limit", default=1000000),
            default=1000000,
            minimum=1000,
        )

    def piece_table_enabled(self) -> bool:
        return _as_bool(self._lookup("features", "piece_table", "enabled", default=True), default=True)

    def piece_table_line_threshold(self) -> int:
        return _as_int(
            self._lookup("features", "piece_table", "large_file_line_threshold", default=50000),
            default=50000,
            minimum=1000,
        )

    def live_grep_enabled(self) -> bool:
        return self.feature_enabled("live_grep")

    def live_grep_max_results(self) -> int:
        return _as_int(
            self._lookup("features", "live_grep", "max_results", default=200),
            default=200,
            minimum=20,
        )

    def lsp_enabled(self) -> bool:
        return self.feature_enabled("lsp")

    def lsp_command(self) -> list[str]:
        value = self._lookup("features", "lsp", "command", default=[])
        if isinstance(value, str):
            return [item for item in shlex.split(value) if item.strip()]
        if isinstance(value, list):
            parsed: list[str] = []
            for item in value:
                if not isinstance(item, str):
                    continue
                clean = item.strip()
                if clean:
                    parsed.append(clean)
            return parsed
        return []

    def lsp_timeout_seconds(self) -> float:
        return _as_float(
            self._lookup("features", "lsp", "timeout_seconds", default=1.2),
            default=1.2,
            minimum=0.2,
        )

    def lsp_language_id_map(self) -> dict[str, str]:
        value = self._lookup("features", "lsp", "language_id_map", default={})
        if not isinstance(value, Mapping):
            return {}
        mapping: dict[str, str] = {}
        for ext, language_id in value.items():
            if not isinstance(ext, str) or not isinstance(language_id, str):
                continue
            clean_ext = ext.strip().lower()
            clean_language = language_id.strip()
            if not clean_ext or not clean_language:
                continue
            if not clean_ext.startswith("."):
                clean_ext = f".{clean_ext}"
            mapping[clean_ext] = clean_language
        return mapping

    def text_objects_enabled(self) -> bool:
        return self.feature_enabled("text_objects")

    def undo_tree_enabled(self) -> bool:
        return self.feature_enabled("undo_tree")

    def undo_tree_max_actions(self) -> int:
        return _as_int(
            self._lookup("features", "undo_tree", "max_actions", default=400),
            default=400,
            minimum=20,
        )

    def macros_enabled(self) -> bool:
        return self.feature_enabled("macros")

    def file_tree_sort_by(self) -> str:
        value = self._lookup("features", "file_tree", "sort_by", default="name")
        if not isinstance(value, str):
            return "name"
        clean = value.strip().lower()
        if clean in {"name", "type", "mtime"}:
            return clean
        return "name"

    def file_tree_filter_query(self) -> str:
        value = self._lookup("features", "file_tree", "filter_query", default="")
        if not isinstance(value, str):
            return ""
        return value.strip()

    def file_tree_show_hidden(self) -> bool:
        return _as_bool(self._lookup("features", "file_tree", "show_hidden", default=False), default=False)

    def swap_enabled(self) -> bool:
        return self.feature_enabled("swap")

    def swap_directory(self) -> Path:
        value = self._lookup("features", "swap", "directory", default="")
        if isinstance(value, str):
            clean = value.strip()
            if clean and clean.lower() not in {"legacy", "default"}:
                resolved = self.resolve_path(clean)
                if resolved is not None:
                    return resolved
        return (self.runtime_directory() / "swap").resolve()

    def swap_interval_seconds(self) -> float:
        return _as_float(
            self._lookup("features", "swap", "interval_seconds", default=4.0),
            default=4.0,
            minimum=0.5,
        )

    def auto_save_enabled(self) -> bool:
        return self.feature_enabled("auto_save")

    def auto_save_interval_seconds(self) -> float:
        return _as_float(
            self._lookup("features", "auto_save", "interval_seconds", default=8.0),
            default=8.0,
            minimum=1.0,
        )

    def session_enabled(self) -> bool:
        return self.feature_enabled("session")

    def session_restore_on_startup(self) -> bool:
        return _as_bool(
            self._lookup("features", "session", "restore_on_startup", default=False),
            default=False,
        )

    def session_file(self) -> Path:
        value = self._lookup("features", "session", "file", default="")
        legacy_names = {".pvim.session.json"}
        if isinstance(value, str):
            clean = value.strip()
            if clean and clean not in legacy_names:
                resolved = self.resolve_path(clean)
                if resolved is not None:
                    return resolved
        return (self.runtime_directory() / "session" / "current.json").resolve()

    def session_profiles_directory(self) -> Path:
        value = self._lookup("features", "session", "profiles_directory", default="")
        legacy_names = {".pvim.sessions"}
        if isinstance(value, str):
            clean = value.strip()
            if clean and clean not in legacy_names:
                resolved = self.resolve_path(clean)
                if resolved is not None:
                    return resolved
        return (self.runtime_directory() / "session" / "profiles").resolve()

    def plugins_directory(self) -> Path:
        value = self._lookup("features", "plugins", "directory", default="plugins")
        legacy_names = {"plugins", ".\\plugins", "./plugins"}
        if isinstance(value, str):
            clean = value.strip()
            if clean and clean not in legacy_names:
                resolved = self.resolve_path(clean)
                if resolved is not None:
                    return resolved
        return (self.runtime_directory() / "plugins").resolve()

    def plugins_auto_load(self) -> bool:
        return _as_bool(self._lookup("features", "plugins", "auto_load", default=True), default=True)

    def experimental_jit_enabled(self) -> bool:
        return _as_bool(self._lookup("performance", "experimental_jit", default=True), default=True)

    def lazy_load_enabled(self) -> bool:
        return _as_bool(self._lookup("performance", "lazy_load", default=True), default=True)

    def profile_top_n(self) -> int:
        return _as_int(self._lookup("performance", "profile_top_n", default=25), default=25, minimum=5)

    def config_reload_enabled(self) -> bool:
        return self.feature_enabled("config_reload")

    def config_reload_interval_seconds(self) -> float:
        return _as_float(
            self._lookup("features", "config_reload", "interval_seconds", default=1.0),
            default=1.0,
            minimum=0.2,
        )

    def quickfix_max_items(self) -> int:
        return _as_int(
            self._lookup("features", "quickfix", "max_items", default=300),
            default=300,
            minimum=20,
        )

    def _parse_autocmd_event_mapping(self, value: Any) -> dict[str, list[str]]:
        if isinstance(value, str):
            clean = value.strip()
            return {"bufreadpost": [clean]} if clean else {}
        if isinstance(value, list):
            commands = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            return {"bufreadpost": commands} if commands else {}
        if not isinstance(value, Mapping):
            return {}
        parsed: dict[str, list[str]] = {}
        for event_name, commands in value.items():
            if not isinstance(event_name, str):
                continue
            event_key = event_name.strip().lower()
            if not event_key:
                continue
            if isinstance(commands, str):
                command_list = [commands.strip()] if commands.strip() else []
            elif isinstance(commands, list):
                command_list = [item.strip() for item in commands if isinstance(item, str) and item.strip()]
            else:
                command_list = []
            parsed[event_key] = command_list
        return parsed

    def autocmd_events(self) -> dict[str, list[str]]:
        if not self.feature_enabled("autocmds"):
            return {}
        value = self._lookup("features", "autocmds", "events", default={})
        return self._parse_autocmd_event_mapping(value)

    def autocmd_filetype_events(self, *, language_id: str = "", extension: str = "") -> dict[str, list[str]]:
        if not self.feature_enabled("autocmds"):
            return {}
        value = self._lookup("features", "autocmds", "filetypes", default={})
        if not isinstance(value, Mapping):
            return {}
        language = language_id.strip().lower()
        suffix = extension.strip().lower().lstrip(".")
        dotted_suffix = f".{suffix}" if suffix else ""
        accepted = {"*"}
        if language:
            accepted.add(language)
        if suffix:
            accepted.add(suffix)
            accepted.add(dotted_suffix)
        merged: dict[str, list[str]] = {}
        for filetype, event_map in value.items():
            if not isinstance(filetype, str):
                continue
            key = filetype.strip().lower()
            if key not in accepted:
                continue
            parsed = self._parse_autocmd_event_mapping(event_map)
            for event, commands in parsed.items():
                target = merged.setdefault(event, [])
                for command in commands:
                    if command not in target:
                        target.append(command)
        return merged

    def scoped_variables_enabled(self) -> bool:
        return self.feature_enabled("scoped_vars")

    def clipboard_enabled(self) -> bool:
        return self.feature_enabled("clipboard")

    def dap_enabled(self) -> bool:
        return self.feature_enabled("dap")

    def dap_python_command(self) -> str:
        value = self._lookup("features", "dap", "python_command", default="python")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return "python"

    def plugin_sandbox_enabled(self) -> bool:
        return self.feature_enabled("plugin_sandbox")

    def plugin_sandbox_allowed_actions(self) -> set[str]:
        defaults = {
            "message",
            "line_count",
            "get_line",
            "set_line",
            "cursor",
            "find",
            "replace_all",
            "current_file",
            "virtual.add",
            "virtual.set",
            "virtual.clear",
            "virtual.get",
            "ast.node_at",
        }
        value = self._lookup("features", "plugin_sandbox", "allow_actions", default=list(defaults))
        if not isinstance(value, list):
            return defaults
        parsed = {str(item).strip() for item in value if isinstance(item, str) and str(item).strip()}
        return parsed or defaults
