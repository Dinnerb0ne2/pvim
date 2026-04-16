from __future__ import annotations

import asyncio
import cProfile
from collections import deque
from dataclasses import dataclass, field
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import pstats
import re
import shutil
import subprocess
import time
import traceback

from ... import APP_NAME, APP_VERSION
from ...core.async_runtime import AsyncRuntime
from ...core.buffer import Buffer
from ...core.config import AppConfig
from ...core.console import KeyReader, TerminalUI
from ...core.display import display_width, index_from_display_col, pad_to_display, slice_by_display
from ...core.history import ActionRecord, ActionSnapshot, HistoryStack
from ...core.persistence import EditorPersistence, SwapPayload
from ...core.process_pipe import AsyncProcessManager
from ...core.terminal_capabilities import detect_terminal_capabilities
from ...core.theme import RESET, Theme, load_theme
from ...core.theme_manager import ThemeManager
from ...ui_grid import AbstractUI
from ...features.ast_query import AstQueryService
from ...features.file_index import FileIndex
from ...features.formatter import normalize_code_style, organize_python_imports
from ...features.fuzzy import fuzzy_filter
from ...features.git_status import GitStatusProvider
from ...features.incremental_syntax import FoldRange, IncrementalSyntaxModel, ParseSummary
from ...features.lsp import LspClient
from ...features.live_grep import GrepMatch, LiveGrep
from ...features.stdlib_bridge import (
    deep_merge_dicts,
    fetch_http_text,
    python_source_summary,
    read_json_mapping,
    validate_required_keys,
)
from ...features.modules import FileTreeFeature, GitControlFeature, TabCompletionFeature
from ...features.modules.git_control import GitSnapshot
from ...features.refactor import find_next, rename_symbol, replace_all, replace_next, word_at_cursor
from ...features.syntax import PLAIN_PROFILE, SyntaxManager
from ...plugins import PluginManager
from ...scripting import ScriptError
from ..floating_list import FloatingList
from ..layout import FeatureDescriptor, FeatureRegistry, LayoutContext, LayoutManager, NotificationCenter
from .commands import CommandsMixin
from .insert_mode import InsertModeMixin
from .modes import (
    MODE_ALERT,
    MODE_COMMAND,
    MODE_COMPLETION,
    MODE_EXPLORER,
    MODE_FLOAT_LIST,
    MODE_FUZZY,
    MODE_INSERT,
    MODE_KEY_HINTS,
    MODE_LIVE_GREP,
    MODE_NORMAL,
    MODE_TERMINAL,
    MODE_VISUAL,
)
from .normal_mode import NormalModeMixin
from .text_objects import is_word_char, quote_range, word_range
from .ui_mode import UIModeMixin


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass(slots=True, frozen=True)
class QuickfixItem:
    path: Path
    line: int
    col: int
    text: str


@dataclass(slots=True)
class TerminalSession:
    process_id: int
    command: str
    output: list[str] = field(default_factory=list)
    scroll: int = 0
    search_query: str = ""
    search_hits: list[int] = field(default_factory=list)
    search_index: int = -1


class PvimEditor(NormalModeMixin, InsertModeMixin, UIModeMixin, CommandsMixin):
    def __init__(self, file_path: Path | None, config: AppConfig, ui: AbstractUI | None = None) -> None:
        self.config = config

        self.buffer = Buffer(lines=[""])
        self.file_path: Path | None = None
        self._workspace_root = Path.cwd().resolve()

        self.cx = 0
        self.cy = 0
        self.row_offset = 0
        self.col_offset = 0

        self.mode = MODE_NORMAL
        self.pending_operator = ""
        self.pending_scope = ""
        self._pending_motion = ""
        self.command_text = ""
        self._command_prompt = ":"
        self.visual_anchor: int | None = None
        self.extra_cursor_lines: list[int] = []

        self.fuzzy_query = ""
        self.fuzzy_matches: list[Path] = []
        self.fuzzy_index = 0
        self._floating_list: FloatingList | None = None
        self._floating_accept_mode = MODE_NORMAL
        self._floating_source = "generic"
        self._completion_prefix = ""
        self._completion_insert_col = 0
        self._completion_insert_row = 0
        self._completion_replace_end = 0
        self._live_grep = LiveGrep()
        self._live_grep_matches: list[GrepMatch] = []
        self._live_grep_requested_version = 0
        self._live_grep_running_version = 0
        self._live_grep_task_active = False
        self._live_grep_query = ""
        self._live_grep_mode = False
        self._lsp_enabled = False
        self._lsp_command: list[str] = []
        self._lsp_timeout_seconds = 1.2
        self._lsp_client: LspClient | None = None
        self._lsp_code_actions: list[dict[str, Any]] = []
        self._lsp_nav_locations: list[tuple[Path, int, int]] = []
        self._jump_back_stack: list[tuple[str, int, int, int, int]] = []
        self._jump_forward_stack: list[tuple[str, int, int, int, int]] = []
        self._jump_history_limit = 240
        self._quickfix_items: list[QuickfixItem] = []
        self._quickfix_index = -1

        self._history = HistoryStack(max_actions=400)
        self._history_enabled = True
        self._history_suspended = False
        self._skip_history_once = False
        self._current_line_ending = "\n"
        self._current_encoding = "utf-8"
        self._encoding_candidates: list[str] = ["utf-8"]

        self._input_queue: deque[str] = deque()
        self._search_history: deque[str] = deque(maxlen=120)
        self._search_history_index = 0
        self._search_history_draft = ""
        self._search_preview_origin: tuple[int, int, int, int] | None = None
        self._incremental_search_query = ""
        self._last_search_query = ""
        self._macro_registers: dict[str, list[str]] = {}
        self._macro_recording_register: str | None = None
        self._macro_recording_keys: list[str] = []
        self._macro_waiting_action: str = ""
        self._macro_replaying = False

        self._persistence = EditorPersistence()
        self._swap_enabled = True
        self._swap_interval = 4.0
        self._last_swap_write = 0.0
        self._auto_save_enabled = True
        self._auto_save_interval = 8.0
        self._last_auto_save = 0.0
        self._swap_prompt_path: Path | None = None
        self._swap_prompt_payload: SwapPayload | None = None
        self._session_enabled = True
        self._session_restore_on_startup = False
        self._runtime_root = Path.cwd().resolve()
        self._session_path = self._runtime_root / "session" / "current.json"
        self._session_profiles_dir = self._runtime_root / "session" / "profiles"
        self._runtime_logger: logging.Logger | None = None
        self._runtime_logger_path: Path | None = None
        self._autocmd_events: dict[str, list[str]] = {}
        self._autocmd_filetype_events: dict[str, list[str]] = {}
        self._autocmd_depth = 0
        self._soft_wrap_enabled = True
        self._global_vars: dict[str, str] = {}
        self._window_vars: dict[str, dict[str, str]] = {}
        self._buffer_vars: dict[str, dict[str, str]] = {}
        self._clipboard_cache = ""
        self._dap_breakpoints: dict[str, set[int]] = {}
        self._dap_session_process_id: int | None = None
        self._dap_target_path: Path | None = None
        self._syntax_model = IncrementalSyntaxModel()
        self._fold_ranges: tuple[FoldRange, ...] = ()
        self._fold_collapsed: set[int] = set()
        self._syntax_parse_summary = ParseSummary(
            changed=False,
            changed_start=-1,
            changed_end=-1,
            parsed_from=-1,
            parsed_lines=0,
        )
        self._incremental_select_ranges: list[tuple[int, int, int, int, str]] = []
        self._incremental_select_index = -1

        self.key_hints_enabled = True
        self.key_hints_trigger = "F1"
        self.key_hint_lines: list[str] = []
        self.key_hint_scroll = 0

        self.alert_lines: list[str] = []
        self._resume_mode_after_alert = MODE_NORMAL

        self.modified = False
        self.running = True

        self.message = "Ready."
        self.message_error = False

        self._last_frame: list[str] = []
        self._last_view_state: tuple[object, ...] | None = None
        self._last_cursor_line = 0
        self._ui = ui if ui is not None else TerminalUI()
        self._ui_entered = False
        self._shutdown_complete = False
        self._async_runtime = AsyncRuntime()
        self._process_manager = AsyncProcessManager(self._async_runtime)
        self._last_tick = time.monotonic()
        self._config_watch_enabled = True
        self._config_watch_interval = 1.0
        self._last_config_watch = 0.0
        self._config_watch_mtimes: dict[str, int] = {}
        self._feature_registry = FeatureRegistry()
        self._layout_manager = LayoutManager(self._feature_registry)
        self._notifications = NotificationCenter()
        self._tab_items: list[str] = []
        self._current_tab_index = 0
        self._git_control_task_active = False
        self._git_control_last_refresh = 0.0
        self._git_control_requested_version = 0
        self._git_control_running_version = 0
        self._file_tree_task_active = False
        self._file_tree_requested_version = 0
        self._file_tree_running_version = 0

        self.show_line_numbers = True
        self.tab_size = 4
        self.show_sidebar = False
        self._sidebar_manual_override = False
        self._project_mode = False
        self.sidebar_width = 30
        self._lazy_load_enabled = True
        self._terminal_capabilities = detect_terminal_capabilities()
        self._unicode_ui = self._terminal_capabilities.unicode_ui

        self.theme: Theme = load_theme(None, self._terminal_capabilities)
        self.syntax: SyntaxManager | None = None
        self._syntax_profile = PLAIN_PROFILE
        self.file_index = FileIndex(self._workspace_root, max_files=3000)
        self.git = GitStatusProvider(self._workspace_root, enabled=False, refresh_seconds=2.0)
        self._ast_query_service: AstQueryService | None = None
        self._file_tree_feature = FileTreeFeature(enabled=False)
        self._completion_feature = TabCompletionFeature(enabled=False)
        self._git_control_feature = GitControlFeature(enabled=False)
        self._terminal_process_id: int | None = None
        self._terminal_output: list[str] = []
        self._terminal_input = ""
        self._terminal_scroll = 0
        self._terminal_sessions: dict[int, TerminalSession] = {}
        self._terminal_session_order: list[int] = []
        self._terminal_split_view = False
        self._theme_manager = ThemeManager(
            builtin_dirs=[Path.cwd(), Path.cwd() / "themes"],
            user_dir=Path.cwd() / "themes",
        )
        self._split_enabled = False
        self._split_orientation = "vertical"
        self._split_ratio = 0.5
        self._split_focus = "main"
        self._split_main_view = (0, 0, 0, 0)
        self._split_secondary_view = (0, 0, 0, 0)
        self.plugins = PluginManager(
            plugins_root=Path.cwd() / "plugins",
            enabled=False,
            step_limit=100_000,
            auto_load=False,
            host_api=self._plugin_api_dispatch,
            sandbox_enabled=self.config.plugin_sandbox_enabled(),
            allowed_actions=self.config.plugin_sandbox_allowed_actions(),
        )
        self._plugins_loaded = False
        self._auto_pairs: dict[str, str] = {}
        self._auto_pair_closers: set[str] = set()
        self._bracket_styles: tuple[str, ...] = ()
        self._bracket_active_style = ""
        self._active_bracket_pair: tuple[int, int, int, int] | None = None
        self._register_feature_descriptors()

        self._apply_runtime_config()
        self._history.set_root_snapshot(self._capture_snapshot())

        if file_path is not None:
            target = file_path.expanduser()
            if target.exists() and target.is_dir():
                self.open_project(target, force=True, startup=True)
            else:
                self.open_file(target, force=True, startup=True)
        elif self._session_enabled and self._session_restore_on_startup:
            self._restore_session()

    @property
    def lines(self) -> list[str]:
        return self.buffer.lines

    @lines.setter
    def lines(self, value: list[str]) -> None:
        self.buffer.set_lines(value)

    def _register_feature_descriptors(self) -> None:
        self._feature_registry.register(
            FeatureDescriptor(
                name="core",
                enabled=True,
                ui_components={"statusline"},
                trigger="always",
            )
        )
        self._feature_registry.register(
            FeatureDescriptor(
                name="tabline",
                enabled=False,
                ui_components={"tabline"},
                trigger="always",
            )
        )
        self._feature_registry.register(
            FeatureDescriptor(
                name="winbar",
                enabled=False,
                ui_components={"winbar"},
                trigger="cursor-move",
            )
        )
        self._feature_registry.register(
            FeatureDescriptor(
                name="file_tree",
                enabled=False,
                ui_components={"float"},
                trigger="command",
            )
        )
        self._feature_registry.register(
            FeatureDescriptor(
                name="tab_completion",
                enabled=False,
                ui_components={"float"},
                trigger="insert-tab",
            )
        )
        self._feature_registry.register(
            FeatureDescriptor(
                name="git_control",
                enabled=False,
                ui_components={"statusline_segment", "virtual_text"},
                trigger="async-refresh",
            )
        )
        self._feature_registry.register(
            FeatureDescriptor(
                name="notifications",
                enabled=False,
                ui_components={"float"},
                trigger="message",
            )
        )
        self._feature_registry.register_status_segment(
            "left",
            "core",
            self._status_left_segment,
        )
        self._feature_registry.register_status_segment(
            "right",
            "core",
            self._status_right_segment,
        )
        self._feature_registry.register_status_segment(
            "center",
            "git_control",
            lambda _context: self._git_control_feature.status_segment(),
        )

    def _status_left_segment(self, _context: LayoutContext) -> str:
        dirty = " [+]" if self.modified else ""
        return f"{self.mode} {self.file_path.name if self.file_path else '[No Name]'}{dirty}"

    def _status_right_segment(self, context: LayoutContext) -> str:
        return f"{self._current_encoding} Ln {context.row}, Col {context.col}"

    def _apply_runtime_config(self) -> None:
        self.show_line_numbers = self.config.show_line_numbers()
        self.tab_size = self.config.tab_size()
        self._soft_wrap_enabled = self.config.soft_wrap_enabled()
        self.show_sidebar = self.config.sidebar_enabled() if self._project_mode else False
        self._sidebar_manual_override = False
        self.sidebar_width = self.config.sidebar_width()
        self.key_hints_enabled = self.config.key_hints_enabled()
        self.key_hints_trigger = self.config.key_hints_trigger()
        self._lazy_load_enabled = self.config.lazy_load_enabled()
        self._swap_enabled = self.config.swap_enabled()
        self._swap_interval = self.config.swap_interval_seconds()
        self._persistence.set_swap_directory(self.config.swap_directory())
        self._auto_save_enabled = self.config.auto_save_enabled()
        self._auto_save_interval = self.config.auto_save_interval_seconds()
        self._encoding_candidates = self.config.preferred_encodings()
        if self._current_encoding not in self._encoding_candidates:
            self._current_encoding = self._encoding_candidates[0]
        self._runtime_root = self.config.runtime_directory()
        self._load_macro_store(noisy=False)
        self._load_shortcut_overrides()
        self._session_enabled = self.config.session_enabled()
        self._session_restore_on_startup = self.config.session_restore_on_startup()
        self._session_path = self.config.session_file()
        self._session_profiles_dir = self.config.session_profiles_directory()
        self._theme_manager = ThemeManager(
            builtin_dirs=[self.config.path.parent, self.config.path.parent / "themes"],
            user_dir=self._runtime_root / "themes",
        )
        self._config_watch_enabled = self.config.config_reload_enabled()
        self._config_watch_interval = self.config.config_reload_interval_seconds()
        self._autocmd_events = self.config.autocmd_events()
        self._refresh_filetype_autocmds(self.file_path)
        self._history_enabled = self.config.undo_tree_enabled()
        self._history.set_limit(self.config.undo_tree_max_actions())
        self._feature_registry.set_enabled("tabline", self.config.feature_enabled("tabline"))
        self._feature_registry.set_enabled("winbar", self.config.feature_enabled("winbar"))
        self._feature_registry.set_enabled("file_tree", self.config.feature_enabled("file_tree"))
        self._feature_registry.set_enabled("tab_completion", self.config.feature_enabled("tab_completion"))
        self._feature_registry.set_enabled("git_control", self.config.feature_enabled("git_control"))
        self._feature_registry.set_enabled("notifications", self.config.feature_enabled("notifications"))
        self._file_tree_feature.enabled = self.config.feature_enabled("file_tree")
        self._file_tree_feature.set_show_hidden(self.config.file_tree_show_hidden())
        self._file_tree_feature.set_sort_mode(self.config.file_tree_sort_by())
        self._file_tree_feature.set_filter_query(self.config.file_tree_filter_query())
        self._completion_feature.enabled = self.config.feature_enabled("tab_completion")
        self._git_control_feature.enabled = self.config.feature_enabled("git_control")
        self._lsp_enabled = self.config.lsp_enabled()
        self._lsp_command = self.config.lsp_command()
        self._lsp_timeout_seconds = self.config.lsp_timeout_seconds()
        self._tab_items = [self.file_path.name] if self.file_path else ["[No Name]"]
        self._current_tab_index = 0
        if not self._file_tree_feature.enabled:
            self._close_explorer()
        if not self._completion_feature.enabled:
            self._close_tab_completion()
        if not self.config.feature_enabled("notifications"):
            self._notifications = NotificationCenter()
        if (not self._lsp_enabled or not self._lsp_command) and self._lsp_client is not None:
            try:
                self._async_runtime.run_sync(self._lsp_client.stop(), timeout=2.0)
            except Exception:
                pass

        theme_file = self.config.theme_file() if self.config.theme_enabled() else None
        self.theme = load_theme(theme_file, self._terminal_capabilities)
        if self._lazy_load_enabled:
            self.syntax = None
            self._syntax_profile = PLAIN_PROFILE
        else:
            self.syntax = SyntaxManager(self.config)
            self._syntax_profile = self.syntax.profile_for_file(self.file_path)

        anchor = self.file_path if self.file_path is not None else self._workspace_root
        self._workspace_root = self._detect_workspace_root(anchor)
        self.file_index = FileIndex(self._workspace_root, max_files=self.config.file_scan_limit())
        self.git = GitStatusProvider(
            self._workspace_root,
            enabled=self.config.feature_enabled("git_status"),
            refresh_seconds=self.config.git_refresh_seconds(),
        )
        self.plugins = PluginManager(
            plugins_root=self.config.plugins_directory(),
            enabled=self.config.feature_enabled("plugins"),
            step_limit=self.config.script_step_limit(),
            auto_load=self.config.plugins_auto_load() and not self._lazy_load_enabled,
            host_api=self._plugin_api_dispatch,
            sandbox_enabled=self.config.plugin_sandbox_enabled(),
            allowed_actions=self.config.plugin_sandbox_allowed_actions(),
        )
        self._plugins_loaded = not self._lazy_load_enabled
        if self._plugins_loaded:
            startup_errors = [item["error"] for item in self.plugins.list_plugins() if item["error"]]
            if startup_errors:
                self._show_alert(f"Plugin startup error: {startup_errors[0]}")
        self.buffer.mark_all_dirty()
        use_piece_table = self.config.piece_table_enabled() and (
            self.file_path is None or len(self.lines) >= self.config.piece_table_line_threshold()
        )
        self.buffer.configure_piece_table(use_piece_table)
        self._sync_incremental_syntax(force=True)
        self._auto_pairs = self._load_auto_pairs()
        self._auto_pair_closers = set(self._auto_pairs.values())
        bracket_styles = [
            self.theme.ui_style("bracket_level_1"),
            self.theme.ui_style("bracket_level_2"),
            self.theme.ui_style("bracket_level_3"),
            self.theme.ui_style("bracket_level_4"),
            self.theme.ui_style("bracket_level_5"),
            self.theme.ui_style("bracket_level_6"),
            self.theme.ui_style("bracket_level_7"),
            self.theme.ui_style("bracket_level_8"),
        ]
        fallback_bracket_styles = [
            self.theme.syntax_style("decorator"),
            self.theme.syntax_style("type"),
            self.theme.syntax_style("function"),
            self.theme.syntax_style("keyword"),
            self.theme.syntax_style("builtin"),
            self.theme.syntax_style("number"),
            self.theme.syntax_style("string"),
            self.theme.syntax_style("constant"),
        ]
        self._bracket_styles = tuple(style for style in bracket_styles if style) or tuple(
            style for style in fallback_bracket_styles if style
        )
        self._bracket_active_style = (
            self.theme.ui_style("bracket_active")
            or self.theme.ui_style("selection")
            or self.theme.ui_style("cursor_line")
            or self.theme.ui_style("mode_insert")
        )
        self._file_tree_feature.unicode_art = self._unicode_ui
        self._config_watch_mtimes = self._snapshot_config_mtimes()

    def _load_auto_pairs(self) -> dict[str, str]:
        defaults = {
            "(": ")",
            "[": "]",
            "{": "}",
            '"': '"',
            "'": "'",
            "`": "`",
        }
        if not self.config.feature_enabled("auto_pairs"):
            return {}

        path = self.config.auto_pairs_file()
        if path is None or not path.exists():
            return defaults

        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Auto-pairs config must be an object: {path}")

        pairs = loaded.get("pairs", {})
        if not isinstance(pairs, dict):
            return defaults

        parsed: dict[str, str] = {}
        for key, value in pairs.items():
            if isinstance(key, str) and isinstance(value, str) and len(key) == 1 and len(value) == 1:
                parsed[key] = value

        return parsed or defaults

    def _config_watch_paths(self) -> list[Path]:
        candidates: list[Path | None] = [
            self.config.path,
            self.config.theme_file() if self.config.theme_enabled() else None,
            self.config.syntax_language_map_file(),
            self.config.syntax_default_file(),
            self.config.auto_pairs_file(),
        ]
        unique: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            if path is None:
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            unique.append(path.resolve())
        return unique

    def _snapshot_config_mtimes(self) -> dict[str, int]:
        snapshot: dict[str, int] = {}
        for path in self._config_watch_paths():
            key = str(path)
            try:
                snapshot[key] = path.stat().st_mtime_ns
            except OSError:
                snapshot[key] = -1
        return snapshot

    def _poll_config_reload(self) -> None:
        if not self._config_watch_enabled:
            return
        now = time.monotonic()
        if now - self._last_config_watch < self._config_watch_interval:
            return
        self._last_config_watch = now

        current = self._snapshot_config_mtimes()
        if not self._config_watch_mtimes:
            self._config_watch_mtimes = current
            return
        changed = set(current.keys()) != set(self._config_watch_mtimes.keys())
        if not changed:
            for key, value in current.items():
                if self._config_watch_mtimes.get(key) != value:
                    changed = True
                    break
        if not changed:
            return

        try:
            self.config = AppConfig.load(self.config.path)
            self._apply_runtime_config()
            self._set_message("Configuration reloaded (detected file changes).")
        except Exception as exc:
            self._set_message(f"Config auto-reload failed: {exc}", error=True)
            self._config_watch_mtimes = current

    def _plugin_api_dispatch(self, object_id: int, action: str, args: list[object]) -> object:
        if action == "message":
            text = str(args[0]) if args else ""
            self._set_message(text)
            return True

        if action == "open":
            if not args:
                return False
            return self.open_file(str(args[0]), force=False)

        if action == "save":
            return self.save_file()

        if action == "line_count":
            return len(self.lines)

        if action == "get_line":
            if not args:
                return ""
            row = int(args[0])
            if row < 1 or row > len(self.lines):
                return ""
            return self.lines[row - 1]

        if action == "set_line":
            if len(args) < 2:
                return False
            row = int(args[0])
            if row < 1 or row > len(self.lines):
                return False
            self.lines[row - 1] = str(args[1])
            self.buffer.mark_dirty(row - 1)
            self._mark_modified()
            return True

        if action == "cursor":
            return f"{self.cy + 1}:{self.cx + 1}"

        if action == "find":
            if not args:
                return False
            return self._find(str(args[0]))

        if action == "replace_all":
            if len(args) < 2:
                return 0
            updated, count = replace_all(self.lines, str(args[0]), str(args[1]))
            if count > 0:
                self.lines = updated
                self._mark_modified()
            return count

        if action == "command":
            if not args:
                return False
            self.execute_command(str(args[0]))
            return True

        if action == "current_file":
            return str(self.file_path) if self.file_path else ""

        if action == "virtual.add":
            if len(args) < 2:
                return False
            row = int(args[0]) - 1
            if row < 0 or row >= len(self.lines):
                return False
            self.buffer.add_virtual_text(row, str(args[1]))
            return True

        if action == "virtual.set":
            if len(args) < 2:
                return False
            row = int(args[0]) - 1
            if row < 0 or row >= len(self.lines):
                return False
            chunks = [str(item) for item in args[1:] if str(item)]
            self.buffer.set_virtual_text(row, chunks)
            return True

        if action == "virtual.clear":
            if not args:
                self.buffer.clear_virtual_text()
                return True
            row = int(args[0]) - 1
            self.buffer.clear_virtual_text(row)
            return True

        if action == "virtual.get":
            if not args:
                return ""
            row = int(args[0]) - 1
            if row < 0 or row >= len(self.lines):
                return ""
            return " | ".join(self.buffer.get_virtual_text(row))

        if action == "ast.node_at":
            row = int(args[0]) if len(args) >= 1 else self.cy + 1
            col = int(args[1]) if len(args) >= 2 else self.cx + 1
            kind_text = str(args[2]) if len(args) >= 3 else "function,class"
            kinds = {item.strip() for item in kind_text.split(",") if item.strip()}
            source = "\n".join(self.lines)
            match = self._ast_query().query_at(
                file_path=self.file_path,
                source=source,
                row=row,
                col=col,
                kinds=kinds or {"function", "class"},
            )
            if match is None:
                return ""
            return match.to_compact()

        if action == "proc.start":
            if not args:
                return 0
            command = str(args[0]).strip()
            if not command:
                return 0
            cwd = str(args[1]) if len(args) > 1 else None
            return self._process_manager.start(command, cwd=cwd)

        if action == "proc.write":
            if len(args) < 2:
                return False
            return self._process_manager.write(int(args[0]), str(args[1]))

        if action == "proc.read":
            if not args:
                return ""
            process_id = int(args[0])
            max_lines = int(args[1]) if len(args) > 1 else 20
            lines = self._process_manager.read(process_id, max_lines=max_lines)
            return "\n".join(lines)

        if action == "proc.stop":
            if not args:
                return False
            return self._process_manager.stop(int(args[0]))

        if action == "proc.status":
            if not args:
                return "unknown"
            return self._process_manager.status(int(args[0]))

        if action == "task.sleep":
            seconds = float(args[0]) if args else 0.0
            label = str(args[1]) if len(args) > 1 else f"sleep:{seconds}"

            async def _sleep_task() -> str:
                await asyncio.sleep(max(0.0, seconds))
                return label

            return self._async_runtime.submit(label, _sleep_task())

        raise ValueError(f"Unknown api action: {action}")

    def _show_alert(self, text: str) -> None:
        message = text.strip()
        if not message:
            message = "Unknown error"
        self.alert_lines = [line for line in message.splitlines() if line] or [message]
        self._resume_mode_after_alert = self.mode if self.mode != MODE_ALERT else MODE_NORMAL
        self.mode = MODE_ALERT
        self._set_message(self.alert_lines[0], error=True)

    def _close_alert(self) -> None:
        self.mode = self._resume_mode_after_alert if self._resume_mode_after_alert != MODE_ALERT else MODE_NORMAL
        self.alert_lines = []
        self._swap_prompt_path = None
        self._swap_prompt_payload = None

    def _open_key_hints(self) -> None:
        if not self.key_hints_enabled:
            self._set_message("Key hints are disabled in config.", error=True)
            return
        self.key_hint_lines = self._build_key_hint_lines()
        self.key_hint_scroll = 0
        self.mode = MODE_KEY_HINTS

    def _build_key_hint_lines(self) -> list[str]:
        lines = [
            "Keyboard Hints",
            "",
            "General",
            "  Ctrl+S save",
            "  Ctrl+Q / Ctrl+C quit",
            f"  {self.key_hints_trigger} open key hints",
            "",
            "Mode switch",
            "  i insert mode",
            "  Esc back to normal",
            "  : command mode",
            "  V visual line mode",
            "",
            "Editing",
            "  Ctrl+/ toggle comment",
            "  Ctrl+D select next same word (fallback: cursor down)",
            "  Ctrl+U clear multi-cursor",
            "  Ctrl+Left / Ctrl+Right move by word",
            "  Tab / Shift+Tab indent / outdent",
            "  % jump to matching bracket",
            "  u undo / Ctrl+R redo (Ctrl+Y also works)",
            "  g- / g+ switch undo-tree branches",
            "  Ctrl+O / gb jump back, gf jump forward",
            "  gd goto definition (LSP/fallback)",
            "  n repeat last search",
            "  q a ... q record macro / @a replay",
            "  ciw da\" vap cif text objects",
            "  dib dab di( da( di[ da[ di{ da{ bracket text objects",
            "  gv / gV incremental selection expand/shrink",
            "  za / zo / zc syntax fold toggle/open/close",
            "",
            "Tools",
            "  Ctrl+F quick find",
            "  Ctrl+G quick replace",
            "  / incremental search preview (Up/Down history)",
            "  Ctrl+N tab completion",
            "  Ctrl+P fuzzy finder",
            "  :grep <text> project search",
            "  :findre / :replacere / :replaceallre regex search-replace",
            "  :replaceproj / :replaceprojre project-wide replace",
            "  F6 rename symbol",
            "  K hover docs (when LSP enabled)",
            "  ga code action (LSP)",
            "  F3 file tree",
            "  F4 toggle sidebar",
            "  F8 normalize code style",
            "  Ctrl+W v/s split, Ctrl+W w switch pane, Ctrl+W q close",
            "",
            "Plugin commands",
            "  :plugin list|load|install|uninstall|run ...",
            "",
            "Script commands",
            "  :script run <file>",
            "  api(pvim, 'task.sleep', sec, label)",
            "",
            "Process commands",
            "  :proc start <command>",
            "  :proc status <id>",
            "  :proc read <id> [max]",
            "  :proc write <id> <text>",
            "  :proc stop <id>",
            "  :virtual add <line> <text>",
            "  :virtual clear [line]",
            "  :ast [line] [col] [function,class]",
            "  :profile script <path>",
            "  :piece",
            "  :termcaps",
            "  :stdlib config-validate|config-merge|http-get|py-analyze|log-tail",
            "  :theme list | :theme <name>",
            "  :workspace",
            "  :runtime show runtime-managed storage paths",
            "  :project <dir> open folder workspace",
            "  :split / :vsplit / :only / :wincmd w|h|l|q",
            "  :term open|new|split|list|use|search built-in terminal sessions",
            "  :git status|diff|blame|stage|unstage|branches|checkout",
            "  :encoding [name]",
            "  :session save|load|list  (optional profile name)",
            "  :swap write|clear",
            "  :keys list|set|unset|conflicts|reset",
            "  :quickfix list|jump|next|prev|first|last|clear|fromgrep|fromdiag",
            "  :fold list|refresh|toggle|open|close|next|prev",
            "  :autocmd list|reload|run <event>",
            "  :var set|get|list|unset ...",
            "  :clip copy|paste|show",
            "  :iselect expand|shrink|reset|list",
            "  :macro list|show|record|stop|play|clear|save|load",
            "  :undo tree|restore <node-id>|branch-prev|branch-next",
            "  :theme install|uninstall|list|<name>",
            "  :tree open|refresh|close|toggle|sort|filter|clear-filter|hidden  (Tab/- fold)",
            "  :feature <name> <on|off>",
            "  :syntax reload",
            "  :help topics|search <keyword>|jump <topic>",
            "  :jump back|forward|list",
            "  :lsp status|start|stop|refs|impl|symbols|wsymbol|rename|format",
            "  :dap status|start|stop|continue|next|step|where|up|down|out|vars|print|console|break ...",
            "  :diag show LSP diagnostics",
        ]
        return lines

    def _shortcut_overrides_file(self) -> Path:
        return (self._runtime_root / "keymaps.json").resolve()

    def _bindings_section(self) -> dict[str, str]:
        features = self.config.data.setdefault("features", {})
        if not isinstance(features, dict):
            features = {}
            self.config.data["features"] = features
        shortcuts = features.setdefault("vscode_shortcuts", {})
        if not isinstance(shortcuts, dict):
            shortcuts = {}
            features["vscode_shortcuts"] = shortcuts
        bindings = shortcuts.setdefault("bindings", {})
        if not isinstance(bindings, dict):
            bindings = {}
            shortcuts["bindings"] = bindings
        return {str(key): str(value) for key, value in bindings.items() if isinstance(key, str) and isinstance(value, str)}

    def _set_bindings_section(self, bindings: dict[str, str]) -> None:
        features = self.config.data.setdefault("features", {})
        if not isinstance(features, dict):
            features = {}
            self.config.data["features"] = features
        shortcuts = features.setdefault("vscode_shortcuts", {})
        if not isinstance(shortcuts, dict):
            shortcuts = {}
            features["vscode_shortcuts"] = shortcuts
        shortcuts["bindings"] = dict(bindings)

    def _load_shortcut_overrides(self) -> None:
        path = self._shortcut_overrides_file()
        if not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(loaded, dict):
            return
        payload = loaded.get("bindings", {})
        if not isinstance(payload, dict):
            return
        bindings = self._bindings_section()
        for action, key in payload.items():
            if not isinstance(action, str) or not isinstance(key, str):
                continue
            clean_action = action.strip()
            clean_key = key.strip().upper()
            if not clean_action or not clean_key:
                continue
            bindings[clean_action] = clean_key
        self._set_bindings_section(bindings)

    def _save_shortcut_overrides(self, bindings: dict[str, str]) -> None:
        path = self._shortcut_overrides_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"bindings": dict(bindings)}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _shortcut_conflicts(self, bindings: dict[str, str]) -> dict[str, list[str]]:
        reverse: dict[str, list[str]] = {}
        for action, key in bindings.items():
            clean_key = key.strip().upper()
            if not clean_key:
                continue
            reverse.setdefault(clean_key, []).append(action)
        return {key: sorted(actions) for key, actions in reverse.items() if len(actions) > 1}

    def _shortcut_lines(self, bindings: dict[str, str]) -> list[str]:
        conflicts = self._shortcut_conflicts(bindings)
        lines: list[str] = []
        for action in sorted(bindings.keys(), key=str.lower):
            key = bindings[action].strip().upper()
            labels = [item for item in conflicts.get(key, []) if item != action]
            hint = f"  [conflict: {', '.join(labels)}]" if labels else ""
            lines.append(f"{action:<22} -> {key}{hint}")
        if not lines:
            return ["(no shortcuts configured)"]
        return lines

    def _handle_keys_command(self, args: list[str]) -> bool:
        if not args:
            self._open_key_hints()
            return True
        action = args[0].strip().lower()
        bindings = self._bindings_section()
        if action in {"list", "ls"}:
            self._show_alert("\n".join(self._shortcut_lines(bindings)))
            return True
        if action in {"conflicts", "check"}:
            conflicts = self._shortcut_conflicts(bindings)
            if not conflicts:
                self._show_alert("No shortcut conflicts.")
                return True
            lines = [f"{key}: {', '.join(actions)}" for key, actions in sorted(conflicts.items())]
            self._show_alert("\n".join(lines))
            return True
        if action == "set":
            if len(args) < 3:
                self._set_message("Usage: :keys set <action> <key>", error=True)
                return False
            target_action = args[1].strip()
            target_key = args[2].strip().upper()
            if not target_action or not target_key:
                self._set_message("Usage: :keys set <action> <key>", error=True)
                return False
            bindings[target_action] = target_key
            self._set_bindings_section(bindings)
            try:
                self._save_shortcut_overrides(bindings)
            except OSError as exc:
                self._set_message(f"Shortcut save failed: {exc}", error=True)
                return False
            self._set_message(f"Shortcut set: {target_action} -> {target_key}")
            return True
        if action in {"unset", "remove", "rm"}:
            if len(args) < 2:
                self._set_message("Usage: :keys unset <action>", error=True)
                return False
            target_action = args[1].strip()
            if not target_action:
                self._set_message("Usage: :keys unset <action>", error=True)
                return False
            if target_action in bindings:
                bindings.pop(target_action, None)
                self._set_bindings_section(bindings)
                try:
                    self._save_shortcut_overrides(bindings)
                except OSError as exc:
                    self._set_message(f"Shortcut save failed: {exc}", error=True)
                    return False
                self._set_message(f"Shortcut unset: {target_action}")
            else:
                self._set_message(f"Shortcut action not found: {target_action}", error=True)
                return False
            return True
        if action == "reset":
            path = self._shortcut_overrides_file()
            path.unlink(missing_ok=True)
            self.config = AppConfig.load(self.config.path)
            self._apply_runtime_config()
            self._set_message("Shortcut overrides reset.")
            return True
        self._set_message("Usage: :keys list|set|unset|conflicts|reset", error=True)
        return False

    def _help_topics_catalog(self) -> dict[str, str]:
        return {
            "quickfix": (
                "Quickfix\n\n"
                ":quickfix list\n"
                ":quickfix jump <index>\n"
                ":quickfix next|prev|first|last\n"
                ":quickfix clear\n"
                ":quickfix fromgrep [query]\n"
                ":quickfix fromdiag"
            ),
            "fold": (
                "Syntax Fold\n\n"
                ":fold list\n"
                ":fold refresh\n"
                ":fold toggle [line]\n"
                ":fold open|close [line]\n"
                ":fold next|prev"
            ),
            "iselect": (
                "Incremental Selection\n\n"
                ":iselect expand\n"
                ":iselect shrink\n"
                ":iselect reset\n"
                ":iselect list\n\n"
                "Normal mode shortcuts: gv (expand), gV (shrink)"
            ),
            "autocmd": (
                "Autocmds\n\n"
                "Configure in pvim.config.json -> features.autocmds.events / features.autocmds.filetypes\n"
                "Supported events: bufreadpre, bufreadpost, bufwritepre, bufwritepost\n"
                "Filetype keys: python / .py / *\n"
                ":autocmd list|reload|run <event>"
            ),
            "var": (
                "Scoped Vars\n\n"
                ":var set <g:name|w:name|b:name> <value>\n"
                ":var get <g:name|w:name|b:name>\n"
                ":var list [g|w|b]\n"
                ":var unset <g:name|w:name|b:name>\n\n"
                "w: scope is isolated by current split pane (main/secondary)."
            ),
            "clip": (
                "Clipboard\n\n"
                ":clip copy [text]\n"
                ":clip paste\n"
                ":clip show\n\n"
                "Adapters: Windows clip/Get-Clipboard, macOS pbcopy/pbpaste, Linux wl-copy/wl-paste/xclip/xsel, fallback tkinter."
            ),
            "dap": (
                "DAP (pdb backend)\n\n"
                ":dap status\n"
                ":dap start [python_file]\n"
                ":dap continue|next|step|where|up|down|out|stop\n"
                ":dap vars|globals|print <expr>\n"
                ":dap console <pdb-command>\n"
                ":dap log [lines]\n"
                ":dap break add|remove|list|clear [line]"
            ),
            "term": (
                "Terminal\n\n"
                ":term open [cmd]\n"
                ":term new [cmd]\n"
                ":term split [on|off|toggle]\n"
                ":term list|use <id|next|prev>|next|prev\n"
                ":term history [lines]\n"
                ":term search <query|next|prev|clear>\n"
                ":term send <text>\n"
                ":term clear [all]\n"
                ":term close|stop [all]"
            ),
            "macro": (
                "Macros\n\n"
                "Normal mode: q{register} ... q, @{register}\n"
                ":macro list|show <reg>|record <reg>|stop\n"
                ":macro play <reg> [count]\n"
                ":macro clear [reg|all]\n"
                ":macro save|load"
            ),
            "undo": (
                "Undo Tree\n\n"
                "u undo, Ctrl+R redo, g- / g+ branch switch\n"
                ":undo tree\n"
                ":undo restore <node-id>\n"
                ":undo branch-prev|branch-next"
            ),
            "stdlib": (
                "Stdlib Toolkit\n\n"
                ":stdlib config-validate <json-path> [required.keys,...]\n"
                ":stdlib config-merge <base-json> <override-json> [output-json]\n"
                ":stdlib http-get <url> [timeout-seconds]\n"
                ":stdlib py-analyze [python-file]\n"
                ":stdlib log-tail [lines]"
            ),
        }

    def _help_topic_aliases(self) -> dict[str, str]:
        return {
            "qf": "quickfix",
            "zf": "fold",
            "isel": "iselect",
            "selection": "iselect",
            "ac": "autocmd",
            "vars": "var",
            "clipboard": "clip",
            "debug": "dap",
            "terminal": "term",
            "macros": "macro",
            "history": "undo",
            "std": "stdlib",
        }

    def _help_topics_overview(self) -> str:
        topics = sorted(self._help_topics_catalog().keys())
        lines = [
            "PVIM Help",
            "",
            "Usage:",
            "  :help topics",
            "  :help search <keyword>",
            "  :help jump <topic>",
            "  :help <topic>",
            "",
            "Topics:",
            "  " + ", ".join(topics),
            "",
            "Commands: :w :q :e :split/:vsplit/:only :wincmd :find/:findre :replace/:replacere "
            ":replaceall/:replaceallre :replaceproj/:replaceprojre :encoding :project :term "
            ":rename :format :fuzzy :grep :tree :theme :feature :workspace :runtime :session :swap "
            ":keys :script :plugin :proc :virtual :ast :profile :piece :termcaps :stdlib :syntax :git :jump "
            ":lsp :diag :codeaction :dap :quickfix :fold :autocmd :var :clip :iselect :macro :undo",
        ]
        return "\n".join(lines)

    def _help_search(self, keyword: str) -> str:
        clean = keyword.strip().lower()
        if not clean:
            return "Usage: :help search <keyword>"
        catalog = self._help_topics_catalog()
        aliases = self._help_topic_aliases()
        rows: list[str] = []
        for topic in sorted(catalog.keys()):
            body = catalog[topic]
            haystack = f"{topic}\n{body}".lower()
            if clean not in haystack:
                continue
            first_line = body.splitlines()[0] if body else topic
            rows.append(f"{topic}: {first_line}")
        for alias, topic in sorted(aliases.items()):
            if clean in alias.lower():
                rows.append(f"{alias} -> {topic}")
        if not rows:
            return f"No help matches for '{keyword}'."
        return "Help search results\n\n" + "\n".join(dict.fromkeys(rows))

    def _help_text(self, topic: str = "") -> str:
        clean = topic.strip().lower()
        if not clean or clean in {"topics", "topic", "list", "index"}:
            return self._help_topics_overview()
        if clean.startswith("search "):
            return self._help_search(clean[7:])
        if clean.startswith("jump "):
            clean = clean[5:].strip()
        if clean.startswith("open "):
            clean = clean[5:].strip()
        aliases = self._help_topic_aliases()
        catalog = self._help_topics_catalog()
        resolved = aliases.get(clean, clean)
        if resolved in catalog:
            return catalog[resolved]
        return self._help_search(clean)

    def _run_autocmds(self, event_name: str) -> None:
        event = event_name.strip().lower()
        if not event:
            return
        commands = list(self._autocmd_events.get(event, []))
        commands.extend(self._autocmd_filetype_events.get(event, []))
        if not commands:
            return
        if self._autocmd_depth >= 4:
            self._set_message(f"Autocmd depth limit reached: {event}", error=True)
            return
        self._autocmd_depth += 1
        try:
            for raw in commands:
                command = raw.strip()
                if not command:
                    continue
                self.execute_command(command)
        finally:
            self._autocmd_depth = max(0, self._autocmd_depth - 1)

    def _handle_autocmd_command(self, args: list[str]) -> bool:
        action = args[0].strip().lower() if args else "list"
        if action in {"list", "ls"}:
            if not self._autocmd_events and not self._autocmd_filetype_events:
                self._show_alert("(no autocmds configured)")
                return True
            lines: list[str] = []
            for event, commands in sorted(self._autocmd_events.items()):
                lines.append(f"[global:{event}]")
                for command in commands:
                    lines.append(f"  {command}")
            for event, commands in sorted(self._autocmd_filetype_events.items()):
                lines.append(f"[filetype:{event}]")
                for command in commands:
                    lines.append(f"  {command}")
            self._show_alert("\n".join(lines))
            return True
        if action in {"reload", "refresh"}:
            self._autocmd_events = self.config.autocmd_events()
            self._refresh_filetype_autocmds(self.file_path)
            self._set_message("Autocmds reloaded.")
            return True
        if action == "run":
            if len(args) < 2:
                self._set_message("Usage: :autocmd run <event>", error=True)
                return False
            event = args[1].strip().lower()
            if not event:
                self._set_message("Usage: :autocmd run <event>", error=True)
                return False
            self._run_autocmds(event)
            self._set_message(f"Autocmd executed: {event}")
            return True
        self._set_message("Usage: :autocmd list|reload|run <event>", error=True)
        return False

    def _refresh_filetype_autocmds(self, path: Path | None) -> None:
        if path is None:
            self._autocmd_filetype_events = {}
            return
        language_id = self._language_id_for_file(path)
        self._autocmd_filetype_events = self.config.autocmd_filetype_events(
            language_id=language_id,
            extension=path.suffix,
        )

    def _current_buffer_scope_key(self) -> str:
        if self.file_path is None:
            return "__no_name__"
        return str(self.file_path.resolve())

    def _current_window_scope_key(self) -> str:
        if not self._split_enabled:
            return "main"
        return self._split_focus if self._split_focus in {"main", "secondary"} else "main"

    def _scope_bucket(self, scope: str) -> dict[str, str] | None:
        if scope == "g":
            return self._global_vars
        if scope == "w":
            key = self._current_window_scope_key()
            return self._window_vars.setdefault(key, {})
        if scope == "b":
            key = self._current_buffer_scope_key()
            return self._buffer_vars.setdefault(key, {})
        return None

    def _parse_scoped_key(self, value: str) -> tuple[str, str] | None:
        text = value.strip()
        if not text:
            return None
        if ":" in text:
            scope, key = text.split(":", 1)
            scope = scope.strip().lower()
            key = key.strip()
        else:
            scope = "g"
            key = text
        if scope not in {"g", "w", "b"}:
            return None
        if not key:
            return None
        return scope, key

    def _handle_var_command(self, args: list[str]) -> bool:
        if not self.config.scoped_variables_enabled():
            self._set_message("Scoped vars are disabled in config.", error=True)
            return False
        if not args:
            self._set_message("Usage: :var set|get|list|unset ...", error=True)
            return False
        action = args[0].strip().lower()
        if action == "set":
            if len(args) < 3:
                self._set_message("Usage: :var set <g:name|w:name|b:name> <value>", error=True)
                return False
            parsed = self._parse_scoped_key(args[1])
            if parsed is None:
                self._set_message("Invalid scoped key. Use g:name/w:name/b:name", error=True)
                return False
            scope, key = parsed
            bucket = self._scope_bucket(scope)
            if bucket is None:
                self._set_message("Invalid variable scope.", error=True)
                return False
            bucket[key] = " ".join(args[2:])
            if scope == "w":
                self._set_message(f"Var set: {scope}:{key}@{self._current_window_scope_key()}")
            else:
                self._set_message(f"Var set: {scope}:{key}")
            return True
        if action == "get":
            if len(args) < 2:
                self._set_message("Usage: :var get <g:name|w:name|b:name>", error=True)
                return False
            parsed = self._parse_scoped_key(args[1])
            if parsed is None:
                self._set_message("Invalid scoped key. Use g:name/w:name/b:name", error=True)
                return False
            scope, key = parsed
            bucket = self._scope_bucket(scope)
            if bucket is None:
                self._set_message("Invalid variable scope.", error=True)
                return False
            if key not in bucket:
                self._set_message(f"Var not found: {scope}:{key}", error=True)
                return False
            if scope == "w":
                self._set_message(f"{scope}:{key}@{self._current_window_scope_key()}={bucket[key]}")
            else:
                self._set_message(f"{scope}:{key}={bucket[key]}")
            return True
        if action in {"list", "ls"}:
            requested = args[1].strip().lower() if len(args) >= 2 else ""
            scopes = [requested] if requested in {"g", "w", "b"} else ["g", "w", "b"]
            lines: list[str] = []
            for scope in scopes:
                if scope == "w":
                    window_keys = sorted(self._window_vars.keys())
                    current_window = self._current_window_scope_key()
                    if requested == "w":
                        window_keys = [current_window]
                    elif current_window not in window_keys:
                        window_keys.append(current_window)
                    for window_key in window_keys:
                        bucket = self._window_vars.setdefault(window_key, {})
                        lines.append(f"[w:{window_key}]")
                        if not bucket:
                            lines.append("  (empty)")
                            continue
                        for key, value in sorted(bucket.items()):
                            lines.append(f"  {key}={value}")
                    continue
                bucket = self._scope_bucket(scope)
                if bucket is None:
                    continue
                lines.append(f"[{scope}]")
                if not bucket:
                    lines.append("  (empty)")
                    continue
                for key, value in sorted(bucket.items()):
                    lines.append(f"  {key}={value}")
            self._show_alert("\n".join(lines) if lines else "(no vars)")
            return True
        if action in {"unset", "rm", "remove"}:
            if len(args) < 2:
                self._set_message("Usage: :var unset <g:name|w:name|b:name>", error=True)
                return False
            parsed = self._parse_scoped_key(args[1])
            if parsed is None:
                self._set_message("Invalid scoped key. Use g:name/w:name/b:name", error=True)
                return False
            scope, key = parsed
            bucket = self._scope_bucket(scope)
            if bucket is None or key not in bucket:
                self._set_message(f"Var not found: {scope}:{key}", error=True)
                return False
            bucket.pop(key, None)
            if scope == "w":
                self._set_message(f"Var unset: {scope}:{key}@{self._current_window_scope_key()}")
            else:
                self._set_message(f"Var unset: {scope}:{key}")
            return True
        self._set_message("Usage: :var set|get|list|unset ...", error=True)
        return False

    def _write_tk_clipboard(self, text: str) -> bool:
        try:
            import tkinter as tk
        except Exception:
            return False
        try:
            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            root.destroy()
        except Exception:
            return False
        return True

    def _read_tk_clipboard(self) -> str | None:
        try:
            import tkinter as tk
        except Exception:
            return None
        try:
            root = tk.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
        except Exception:
            return None
        return text.replace("\r\n", "\n")

    def _write_system_clipboard(self, text: str) -> bool:
        if not self.config.clipboard_enabled():
            return False
        if os.name == "nt":
            try:
                subprocess.run(
                    ["clip"],
                    input=text,
                    text=True,
                    encoding="utf-8",
                    timeout=0.8,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return True
        if shutil.which("pbcopy"):
            try:
                subprocess.run(
                    ["pbcopy"],
                    input=text,
                    text=True,
                    encoding="utf-8",
                    timeout=0.8,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return True
        for command in (
            ["wl-copy", "--type", "text/plain"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if shutil.which(command[0]) is None:
                continue
            try:
                subprocess.run(
                    command,
                    input=text,
                    text=True,
                    encoding="utf-8",
                    timeout=0.8,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            return True
        return self._write_tk_clipboard(text)

    def _read_system_clipboard(self) -> str | None:
        if not self.config.clipboard_enabled():
            return None
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=0.8,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode != 0:
                return None
            return result.stdout.replace("\r\n", "\n")
        if shutil.which("pbpaste"):
            try:
                result = subprocess.run(
                    ["pbpaste"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=0.8,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode == 0:
                return result.stdout.replace("\r\n", "\n")
            return None
        for command in (
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ):
            if shutil.which(command[0]) is None:
                continue
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=0.8,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                return result.stdout.replace("\r\n", "\n")
        return self._read_tk_clipboard()

    def _insert_clipboard_text(self, text: str) -> bool:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized:
            return False
        line = self._line()
        left = line[: self.cx]
        right = line[self.cx :]
        parts = normalized.split("\n")
        if len(parts) == 1:
            self._set_line(left + parts[0] + right)
            self.cx += len(parts[0])
            self._mark_modified()
            return True
        updated = [left + parts[0], *parts[1:-1], parts[-1] + right]
        self.lines[self.cy : self.cy + 1] = updated
        self.buffer.mark_all_dirty()
        self.cy += len(updated) - 1
        self.cx = len(parts[-1])
        self._mark_modified()
        return True

    def _handle_clipboard_command(self, args: list[str]) -> bool:
        if not args:
            self._set_message("Usage: :clip copy|paste|show [text]", error=True)
            return False
        action = args[0].strip().lower()
        if action in {"copy", "yank"}:
            text = " ".join(args[1:]) if len(args) >= 2 else self._line()
            self._clipboard_cache = text
            self._write_system_clipboard(text)
            self._set_message(f"Clipboard copied ({len(text)} chars).")
            return True
        if action == "paste":
            text = self._read_system_clipboard()
            if text is None:
                text = self._clipboard_cache
            if not text:
                self._set_message("Clipboard is empty.", error=True)
                return False
            if not self._insert_clipboard_text(text):
                self._set_message("Clipboard paste failed.", error=True)
                return False
            self._set_message("Clipboard pasted.")
            return True
        if action in {"show", "peek"}:
            text = self._read_system_clipboard()
            if text is None:
                text = self._clipboard_cache
            if not text:
                self._show_alert("(clipboard is empty)")
                return True
            self._show_alert(text[:7000])
            return True
        self._set_message("Usage: :clip copy|paste|show [text]", error=True)
        return False

    def _runtime_log_file(self) -> Path:
        return (self._runtime_root / "logs" / "pvim.log").resolve()

    def _ensure_runtime_logger(self) -> logging.Logger:
        path = self._runtime_log_file()
        if self._runtime_logger is not None and self._runtime_logger_path == path:
            return self._runtime_logger
        path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("pvim.runtime")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        handler = RotatingFileHandler(path, maxBytes=512_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        self._runtime_logger = logger
        self._runtime_logger_path = path
        return logger

    def _runtime_log(self, level: int, message: str) -> None:
        try:
            logger = self._ensure_runtime_logger()
            logger.log(level, message)
        except OSError:
            pass

    def _runtime_log_tail(self, lines: int) -> list[str]:
        path = self._runtime_log_file()
        if not path.exists():
            return []
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return []
        split = content.splitlines()
        return split[-max(1, lines) :]

    def _handle_stdlib_command(self, args: list[str]) -> bool:
        if not args:
            self._set_message(
                "Usage: :stdlib config-validate|config-merge|http-get|py-analyze|log-tail ...",
                error=True,
            )
            return False
        action = args[0].strip().lower()

        if action in {"config-validate", "validate"}:
            if len(args) < 2:
                self._set_message("Usage: :stdlib config-validate <json-path> [required.keys,...]", error=True)
                return False
            target = self._resolve_path(args[1])
            required_keys: list[str] = ["python.required", "editor.tab_size"]
            if len(args) >= 3:
                required_keys = []
                for chunk in args[2:]:
                    parts = [item.strip() for item in chunk.split(",") if item.strip()]
                    required_keys.extend(parts)
            try:
                payload = read_json_mapping(str(target))
            except Exception as exc:
                self._set_message(f"Config validate failed: {exc}", error=True)
                self._runtime_log(logging.ERROR, f"config-validate failed: {target} -> {exc}")
                return False
            missing = validate_required_keys(payload, required_keys)
            if missing:
                self._set_message(f"Config missing keys: {', '.join(missing)}", error=True)
                self._runtime_log(logging.WARNING, f"config-validate missing keys in {target}: {missing}")
                return False
            self._set_message(f"Config valid: {target.name}")
            self._runtime_log(logging.INFO, f"config-validate ok: {target}")
            return True

        if action in {"config-merge", "merge"}:
            if len(args) < 3:
                self._set_message("Usage: :stdlib config-merge <base-json> <override-json> [output-json]", error=True)
                return False
            base_path = self._resolve_path(args[1])
            override_path = self._resolve_path(args[2])
            output_path = self._resolve_path(args[3]) if len(args) >= 4 else None
            try:
                base_payload = read_json_mapping(str(base_path))
                override_payload = read_json_mapping(str(override_path))
                merged = deep_merge_dicts(base_payload, override_payload)
            except Exception as exc:
                self._set_message(f"Config merge failed: {exc}", error=True)
                self._runtime_log(logging.ERROR, f"config-merge failed: {base_path} + {override_path} -> {exc}")
                return False
            rendered = json.dumps(merged, ensure_ascii=False, indent=2)
            if output_path is not None:
                try:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(rendered, encoding="utf-8")
                except OSError as exc:
                    self._set_message(f"Config merge write failed: {exc}", error=True)
                    self._runtime_log(logging.ERROR, f"config-merge write failed: {output_path} -> {exc}")
                    return False
                self._set_message(f"Config merged: {output_path}")
                self._runtime_log(logging.INFO, f"config-merge wrote: {output_path}")
                return True
            self._show_alert(rendered[:7000])
            self._set_message("Config merged (preview).")
            self._runtime_log(logging.INFO, "config-merge preview")
            return True

        if action in {"http-get", "http"}:
            if len(args) < 2:
                self._set_message("Usage: :stdlib http-get <url> [timeout-seconds]", error=True)
                return False
            url = args[1].strip()
            timeout = 5.0
            if len(args) >= 3:
                try:
                    timeout = max(0.2, float(args[2]))
                except ValueError:
                    self._set_message("Usage: :stdlib http-get <url> [timeout-seconds]", error=True)
                    return False
            try:
                result = fetch_http_text(url, timeout=timeout)
            except Exception as exc:
                self._set_message(f"HTTP request failed: {exc}", error=True)
                self._runtime_log(logging.ERROR, f"http-get failed: {url} -> {exc}")
                return False
            body = result.body
            if "json" in result.content_type.lower():
                try:
                    body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            preview = body[:7000]
            self._show_alert(f"HTTP {result.status} {result.url}\n{result.content_type}\n\n{preview}")
            self._set_message(f"HTTP {result.status}: {result.url}")
            self._runtime_log(logging.INFO, f"http-get {result.status}: {result.url}")
            return True

        if action in {"py-analyze", "analyze"}:
            if len(args) >= 2:
                target = self._resolve_path(args[1])
            else:
                if self.file_path is None:
                    self._set_message("Usage: :stdlib py-analyze <python-file>", error=True)
                    return False
                target = self.file_path
            try:
                source = target.read_text(encoding="utf-8")
                summary = python_source_summary(source)
            except Exception as exc:
                self._set_message(f"Analyze failed: {exc}", error=True)
                self._runtime_log(logging.ERROR, f"py-analyze failed: {target} -> {exc}")
                return False
            self._show_alert(
                "\n".join(
                    [
                        f"file: {target}",
                        f"lines: {summary['lines']}",
                        f"functions: {summary['functions']}",
                        f"classes: {summary['classes']}",
                        f"imports: {summary['imports']}",
                        f"tokens: {summary['tokens']}",
                    ]
                )
            )
            self._set_message(f"Analyze done: {target.name}")
            self._runtime_log(logging.INFO, f"py-analyze: {target}")
            return True

        if action in {"log-tail", "logs"}:
            count = 40
            if len(args) >= 2:
                try:
                    count = max(1, int(args[1]))
                except ValueError:
                    self._set_message("Usage: :stdlib log-tail [lines]", error=True)
                    return False
            rows = self._runtime_log_tail(count)
            if not rows:
                self._show_alert("(runtime log is empty)")
                return True
            self._show_alert("\n".join(rows))
            self._set_message(f"Runtime log tail: {len(rows)} line(s)")
            return True

        self._set_message(
            "Usage: :stdlib config-validate|config-merge|http-get|py-analyze|log-tail ...",
            error=True,
        )
        return False

    def _quickfix_label(self, item: QuickfixItem) -> str:
        try:
            relative = item.path.resolve().relative_to(self._workspace_root.resolve())
            path_label = str(relative)
        except ValueError:
            path_label = str(item.path)
        text = item.text.strip()
        return f"{path_label}:{item.line}:{item.col} {text}".rstrip()

    def _set_quickfix_items(self, items: list[QuickfixItem], *, source: str) -> None:
        limit = self.config.quickfix_max_items()
        trimmed = items[:limit]
        self._quickfix_items = trimmed
        self._quickfix_index = 0 if trimmed else -1
        self._set_message(f"Quickfix ({source}): {len(trimmed)} item(s)")

    def _quickfix_jump(self, index: int) -> bool:
        if not self._quickfix_items:
            self._set_message("Quickfix list is empty.", error=True)
            return False
        if index < 0 or index >= len(self._quickfix_items):
            self._set_message("Quickfix index out of range.", error=True)
            return False
        item = self._quickfix_items[index]
        self._record_jump_origin()
        if not self.open_file(item.path, force=False):
            self._set_message(f"Quickfix open failed: {item.path}", error=True)
            return False
        self.cy = clamp(item.line - 1, 0, len(self.lines) - 1)
        self.cx = clamp(item.col - 1, 0, len(self._line()))
        self._quickfix_index = index
        self._set_message(f"Quickfix {index + 1}/{len(self._quickfix_items)}: {self._quickfix_label(item)}")
        return True

    def _quickfix_shift(self, delta: int) -> bool:
        if not self._quickfix_items:
            self._set_message("Quickfix list is empty.", error=True)
            return False
        if self._quickfix_index < 0:
            self._quickfix_index = 0
        target = (self._quickfix_index + delta) % len(self._quickfix_items)
        return self._quickfix_jump(target)

    def _quickfix_from_live_grep(self, query: str) -> bool:
        clean = query.strip()
        if clean:
            try:
                matches = self._async_runtime.run_sync(
                    self._live_grep.search(self._workspace_root, clean, limit=self.config.quickfix_max_items()),
                    timeout=3.0,
                )
            except Exception as exc:
                self._set_message(f"Quickfix grep failed: {exc}", error=True)
                return False
        else:
            matches = list(self._live_grep_matches)
        if not matches:
            self._set_message("Quickfix grep has no results.", error=True)
            return False
        items = [
            QuickfixItem(
                path=match.file_path.resolve(),
                line=max(1, int(match.line)),
                col=max(1, int(match.column)),
                text=match.text,
            )
            for match in matches
        ]
        self._set_quickfix_items(items, source="grep")
        return True

    def _quickfix_from_diagnostics(self) -> bool:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return False
        try:
            payload = self._async_runtime.run_sync(
                client.diagnostics_raw(self.file_path),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"Quickfix diagnostics failed: {exc}", error=True)
            return False
        if not payload:
            self._set_message("Quickfix diagnostics has no results.", error=True)
            return False
        items: list[QuickfixItem] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message", "")).strip()
            line = 1
            col = 1
            range_obj = item.get("range")
            if isinstance(range_obj, dict):
                start = range_obj.get("start")
                if isinstance(start, dict):
                    try:
                        line = int(start.get("line", 0)) + 1
                    except (TypeError, ValueError):
                        line = 1
                    try:
                        col = int(start.get("character", 0)) + 1
                    except (TypeError, ValueError):
                        col = 1
            items.append(
                QuickfixItem(
                    path=self.file_path.resolve(),
                    line=max(1, line),
                    col=max(1, col),
                    text=message or "diagnostic",
                )
            )
        if not items:
            self._set_message("Quickfix diagnostics has no results.", error=True)
            return False
        self._set_quickfix_items(items, source="diagnostics")
        return True

    def _handle_quickfix_command(self, args: list[str]) -> bool:
        if not self.config.feature_enabled("quickfix"):
            self._set_message("Quickfix is disabled in config.", error=True)
            return False
        action = args[0].strip().lower() if args else "list"
        if action in {"list", "ls"}:
            if not self._quickfix_items:
                self._show_alert("(quickfix is empty)")
                return True
            lines = [
                (f"{index + 1:>3}. {self._quickfix_label(item)}")
                for index, item in enumerate(self._quickfix_items)
            ]
            self._show_alert("\n".join(lines))
            return True
        if action in {"next", "n"}:
            return self._quickfix_shift(1)
        if action in {"prev", "previous", "p"}:
            return self._quickfix_shift(-1)
        if action in {"jump", "j", "open"}:
            if len(args) < 2:
                self._set_message("Usage: :quickfix jump <index>", error=True)
                return False
            try:
                index = int(args[1]) - 1
            except ValueError:
                self._set_message("Usage: :quickfix jump <index>", error=True)
                return False
            return self._quickfix_jump(index)
        if action in {"first", "head"}:
            return self._quickfix_jump(0)
        if action in {"last", "tail"}:
            return self._quickfix_jump(len(self._quickfix_items) - 1)
        if action == "clear":
            self._quickfix_items = []
            self._quickfix_index = -1
            self._set_message("Quickfix cleared.")
            return True
        if action in {"fromgrep", "grep"}:
            return self._quickfix_from_live_grep(" ".join(args[1:]) if len(args) >= 2 else "")
        if action in {"fromdiag", "diag", "diagnostics"}:
            return self._quickfix_from_diagnostics()
        self._set_message(
            "Usage: :quickfix list|jump <index>|next|prev|first|last|clear|fromgrep [query]|fromdiag",
            error=True,
        )
        return False

    def _dap_path_key(self, path: Path) -> str:
        return str(path.resolve())

    def _dap_breakpoint_set(self, path: Path) -> set[int]:
        key = self._dap_path_key(path)
        return self._dap_breakpoints.setdefault(key, set())

    def _dap_active_session(self) -> TerminalSession | None:
        if self._dap_session_process_id is None:
            return None
        return self._terminal_sessions.get(self._dap_session_process_id)

    def _dap_is_running(self) -> bool:
        if self._dap_session_process_id is None:
            return False
        return self._process_manager.status(self._dap_session_process_id) == "running"

    def _dap_send_console(self, command: str) -> bool:
        clean = command.strip()
        if not clean:
            self._set_message("DAP console command is empty.", error=True)
            return False
        session = self._dap_active_session()
        if session is None or not self._dap_is_running():
            self._set_message("DAP is not running.", error=True)
            return False
        active_before = self._terminal_process_id
        self._switch_terminal_session(session.process_id)
        self._send_terminal_input(clean)
        if active_before is not None and active_before != session.process_id:
            self._switch_terminal_session(active_before)
        return True

    def _dap_sync_breakpoint(self, *, path: Path, line: int, add: bool) -> None:
        if not self._dap_is_running():
            return
        session = self._dap_active_session()
        if session is None:
            return
        resolved = path.resolve()
        if self._dap_target_path is not None and resolved == self._dap_target_path:
            command = f"b {line}" if add else f"cl {line}"
        else:
            command = f"b {resolved}:{line}" if add else f"cl {resolved}:{line}"
        active_before = self._terminal_process_id
        self._switch_terminal_session(session.process_id)
        self._send_terminal_input(command)
        if active_before is not None and active_before != session.process_id:
            self._switch_terminal_session(active_before)

    def _handle_dap_command(self, args: list[str]) -> bool:
        if not self.config.dap_enabled():
            self._set_message("DAP is disabled in config.", error=True)
            return False
        action = args[0].strip().lower() if args else "status"
        if action == "status":
            running = self._dap_is_running()
            total_breakpoints = sum(len(lines) for lines in self._dap_breakpoints.values())
            session_id = self._dap_session_process_id if self._dap_session_process_id is not None else "-"
            target = self._dap_target_path.name if self._dap_target_path is not None else "-"
            self._set_message(
                f"DAP(pdb): {'running' if running else 'idle'} "
                f"| session={session_id} target={target} breakpoints={total_breakpoints}"
            )
            return True
        if action in {"start", "run"}:
            target_text = " ".join(args[1:]).strip() if len(args) >= 2 else ""
            if target_text:
                target = self._resolve_path(target_text)
            elif self.file_path is not None:
                target = self.file_path
            else:
                self._set_message("Usage: :dap start <python_file>", error=True)
                return False
            if not target.exists() or target.is_dir():
                self._set_message(f"DAP target not found: {target}", error=True)
                return False
            python_cmd = self.config.dap_python_command()
            command = f'{python_cmd} -m pdb "{target}"'
            if not self._open_terminal(command, force_new=True):
                return False
            session = self._active_terminal_session()
            if session is None:
                self._set_message("DAP start failed: terminal session missing.", error=True)
                return False
            resolved_target = target.resolve()
            self._dap_session_process_id = session.process_id
            self._dap_target_path = resolved_target
            for line in sorted(self._dap_breakpoint_set(resolved_target)):
                self._dap_send_console(f"b {line}")
            self._set_message(f"DAP started: {target.name}")
            return True
        if action in {"stop", "close"}:
            process_id = self._dap_session_process_id
            if process_id is None:
                self._set_message("DAP is not running.", error=True)
                return False
            self._process_manager.stop_sync(process_id, timeout=0.6)
            session = self._terminal_sessions.get(process_id)
            if session is not None:
                session.output.append("[dap] stop requested")
                if len(session.output) > 2000:
                    session.output = session.output[-2000:]
            if self._terminal_process_id == process_id:
                self._close_terminal(kill=False)
            self._dap_session_process_id = None
            self._dap_target_path = None
            self._set_message("DAP stopped.")
            return True
        if action in {"continue", "c"}:
            if not self._dap_send_console("c"):
                return False
            self._set_message("DAP continue.")
            return True
        if action in {"next", "n"}:
            if not self._dap_send_console("n"):
                return False
            self._set_message("DAP next.")
            return True
        if action in {"step", "s"}:
            if not self._dap_send_console("s"):
                return False
            self._set_message("DAP step.")
            return True
        if action in {"where", "bt", "stack"}:
            if not self._dap_send_console("w"):
                return False
            self._set_message("DAP stack trace requested.")
            return True
        if action in {"up", "down", "out"}:
            command = "u" if action == "up" else ("d" if action == "down" else "r")
            if not self._dap_send_console(command):
                return False
            self._set_message(f"DAP {action}.")
            return True
        if action in {"vars", "locals"}:
            if not self._dap_send_console("p locals()"):
                return False
            self._set_message("DAP locals requested.")
            return True
        if action == "globals":
            if not self._dap_send_console("p globals()"):
                return False
            self._set_message("DAP globals requested.")
            return True
        if action in {"print", "eval", "p"}:
            if len(args) < 2:
                self._set_message("Usage: :dap print <expr>", error=True)
                return False
            if not self._dap_send_console(f"p {' '.join(args[1:])}"):
                return False
            self._set_message("DAP expression requested.")
            return True
        if action in {"console", "cmd"}:
            if len(args) < 2:
                self._set_message("Usage: :dap console <pdb-command>", error=True)
                return False
            if not self._dap_send_console(" ".join(args[1:])):
                return False
            self._set_message("DAP console command sent.")
            return True
        if action in {"log", "history"}:
            session = self._dap_active_session()
            if session is None:
                self._set_message("DAP is not running.", error=True)
                return False
            count = 40
            if len(args) >= 2:
                try:
                    count = max(1, int(args[1]))
                except ValueError:
                    self._set_message("Usage: :dap log [lines]", error=True)
                    return False
            lines = session.output[-count:]
            self._show_alert("\n".join(lines) if lines else "(dap log is empty)")
            return True
        if action in {"break", "breakpoint", "bp"}:
            sub = args[1].strip().lower() if len(args) >= 2 else "list"
            if sub in {"list", "ls"} and len(args) == 2:
                lines: list[str] = []
                for path_text in sorted(self._dap_breakpoints.keys()):
                    entries = sorted(self._dap_breakpoints.get(path_text, set()))
                    if not entries:
                        continue
                    name = Path(path_text).name
                    for line in entries:
                        lines.append(f"{name}:{line}")
                self._show_alert("\n".join(lines) if lines else "(no breakpoints)")
                return True
            if self.file_path is None:
                self._set_message("Open a file before managing breakpoints.", error=True)
                return False
            target = self.file_path.resolve()
            breaks = self._dap_breakpoint_set(target)
            if sub == "add":
                line = self.cy + 1
                if len(args) >= 3:
                    try:
                        line = max(1, int(args[2]))
                    except ValueError:
                        self._set_message("Usage: :dap break add [line]", error=True)
                        return False
                breaks.add(line)
                self._dap_sync_breakpoint(path=target, line=line, add=True)
                self._set_message(f"DAP breakpoint added: {target.name}:{line}")
                return True
            if sub in {"remove", "rm", "del"}:
                line = self.cy + 1
                if len(args) >= 3:
                    try:
                        line = max(1, int(args[2]))
                    except ValueError:
                        self._set_message("Usage: :dap break remove [line]", error=True)
                        return False
                if line in breaks:
                    breaks.remove(line)
                    self._dap_sync_breakpoint(path=target, line=line, add=False)
                    self._set_message(f"DAP breakpoint removed: {target.name}:{line}")
                    return True
                self._set_message(f"DAP breakpoint not found: {target.name}:{line}", error=True)
                return False
            if sub in {"clear", "all"}:
                if len(args) >= 3 and args[2].strip().lower() == "all":
                    for path_text, lines in self._dap_breakpoints.items():
                        path_value = Path(path_text)
                        for line in sorted(lines):
                            self._dap_sync_breakpoint(path=path_value, line=line, add=False)
                        lines.clear()
                    self._set_message("DAP breakpoints cleared: all files")
                    return True
                for line in sorted(breaks):
                    self._dap_sync_breakpoint(path=target, line=line, add=False)
                breaks.clear()
                self._set_message(f"DAP breakpoints cleared: {target.name}")
                return True
            if sub in {"list", "ls"}:
                if not breaks:
                    self._show_alert("(no breakpoints)")
                    return True
                lines = [f"{target.name}:{line}" for line in sorted(breaks)]
                self._show_alert("\n".join(lines))
                return True
            self._set_message("Usage: :dap break add|remove|list|clear [line]", error=True)
            return False
        self._set_message(
            "Usage: :dap status|start [file]|stop|continue|next|step|where|up|down|out|"
            "vars|globals|print <expr>|console <cmd>|log [lines]|break add|remove|list|clear [line]",
            error=True,
        )
        return False

    def _sync_incremental_syntax(self, *, force: bool = False) -> ParseSummary:
        summary = self._syntax_model.update(self.lines)
        if not summary.changed and not force:
            return summary
        self._fold_ranges = self._syntax_model.folds()
        valid_starts = {item.start_line for item in self._fold_ranges}
        if self._fold_collapsed:
            self._fold_collapsed = {line for line in self._fold_collapsed if line in valid_starts}
        self._syntax_parse_summary = summary
        return summary

    def _fold_range_at_line(self, line_index: int) -> FoldRange | None:
        if line_index < 0 or line_index >= len(self.lines):
            return None
        direct = self._syntax_model.fold_starting_at(line_index)
        if direct is not None:
            return direct
        return self._syntax_model.enclosing_fold(line_index)

    def _handle_fold_command(self, args: list[str]) -> bool:
        action = args[0].strip().lower() if args else "list"
        if action in {"refresh", "rebuild"}:
            summary = self._sync_incremental_syntax(force=True)
            self._set_message(
                f"Fold rules refreshed: parsed_from={summary.parsed_from + 1 if summary.parsed_from >= 0 else '-'} "
                f"lines={summary.parsed_lines} folds={len(self._fold_ranges)}"
            )
            return True

        if action in {"list", "ls"}:
            if not self._fold_ranges:
                self._show_alert("(no syntax folds)")
                return True
            lines: list[str] = []
            for item in self._fold_ranges[:300]:
                state = "closed" if item.start_line in self._fold_collapsed else "open"
                size = item.end_line - item.start_line
                lines.append(
                    f"{item.start_line + 1:>4}-{item.end_line + 1:<4} {item.kind:<10} size={size:<3} {state}"
                )
            self._show_alert("\n".join(lines))
            return True

        if action in {"toggle", "open", "close"}:
            line = self.cy + 1
            if len(args) >= 2:
                try:
                    line = max(1, int(args[1]))
                except ValueError:
                    self._set_message("Usage: :fold toggle|open|close [line]", error=True)
                    return False
            fold = self._fold_range_at_line(line - 1)
            if fold is None:
                self._set_message(f"No fold at line {line}.", error=True)
                return False
            if action == "open":
                self._fold_collapsed.discard(fold.start_line)
                self._set_message(f"Fold opened: {fold.start_line + 1}-{fold.end_line + 1}")
                return True
            if action == "close":
                self._fold_collapsed.add(fold.start_line)
                self._set_message(f"Fold closed: {fold.start_line + 1}-{fold.end_line + 1}")
                return True
            if fold.start_line in self._fold_collapsed:
                self._fold_collapsed.discard(fold.start_line)
                self._set_message(f"Fold opened: {fold.start_line + 1}-{fold.end_line + 1}")
            else:
                self._fold_collapsed.add(fold.start_line)
                self._set_message(f"Fold closed: {fold.start_line + 1}-{fold.end_line + 1}")
            return True

        if action in {"next", "n"}:
            for fold in self._fold_ranges:
                if fold.start_line > self.cy:
                    self.cy = fold.start_line
                    self.cx = min(self.cx, len(self._line()))
                    self._set_message(f"Fold jump: {fold.start_line + 1}-{fold.end_line + 1}")
                    return True
            self._set_message("No next fold.", error=True)
            return False

        if action in {"prev", "previous", "p"}:
            for fold in reversed(self._fold_ranges):
                if fold.start_line < self.cy:
                    self.cy = fold.start_line
                    self.cx = min(self.cx, len(self._line()))
                    self._set_message(f"Fold jump: {fold.start_line + 1}-{fold.end_line + 1}")
                    return True
            self._set_message("No previous fold.", error=True)
            return False

        if action in {"info", "status"}:
            summary = self._syntax_parse_summary
            changed = "yes" if summary.changed else "no"
            self._set_message(
                f"Syntax incremental: changed={changed} parsed_from={summary.parsed_from + 1 if summary.parsed_from >= 0 else '-'} "
                f"parsed_lines={summary.parsed_lines} folds={len(self._fold_ranges)}"
            )
            return True

        self._set_message("Usage: :fold list|refresh|toggle|open|close|next|prev|info [line]", error=True)
        return False

    def _reset_incremental_selection(self) -> None:
        self._incremental_select_ranges = []
        self._incremental_select_index = -1

    def _selection_candidates(self) -> list[tuple[int, int, int, int, str]]:
        if not self.lines:
            return []
        max_line = len(self.lines) - 1
        candidates: list[tuple[int, int, int, int, str]] = []
        line = self._line()

        word = word_range(line, self.cx, "i")
        if word is not None:
            candidates.append((self.cy, word[0], self.cy, word[1], "word"))

        quote = quote_range(line, self.cx, '"', "a")
        if quote is not None:
            candidates.append((self.cy, quote[0], self.cy, quote[1], "quote"))

        bracket = self._bracket_text_object_range("a", "b")
        if bracket is not None:
            candidates.append((bracket[0], bracket[1], bracket[2], bracket[3], "bracket"))

        paragraph = self._paragraph_range("a")
        if paragraph is not None:
            start_line, end_line = paragraph
            candidates.append((start_line, 0, end_line, len(self.lines[end_line]), "paragraph"))

        fold = self._syntax_model.enclosing_fold(self.cy)
        if fold is not None:
            candidates.append((fold.start_line, 0, fold.end_line, len(self.lines[fold.end_line]), "fold"))

        for kind in ("function", "class"):
            ast_range = self._ast_text_object_range(kind, "a")
            if ast_range is None:
                continue
            candidates.append((ast_range[0], 0, ast_range[2], len(self.lines[ast_range[2]]), f"ast:{kind}"))

        candidates.append((self.cy, 0, self.cy, len(line), "line"))
        candidates.append((0, 0, max_line, len(self.lines[max_line]), "buffer"))

        unique: dict[tuple[int, int, int, int], tuple[int, int, int, int, str]] = {}
        for item in candidates:
            key = item[:4]
            unique.setdefault(key, item)

        def _span_size(item: tuple[int, int, int, int, str]) -> tuple[int, int]:
            return (item[2] - item[0], item[3] - item[1])

        ordered = sorted(unique.values(), key=_span_size)
        return ordered

    def _apply_visual_range(self, target: tuple[int, int, int, int, str]) -> None:
        start_line, _start_col, end_line, end_col, label = target
        start_line = clamp(start_line, 0, len(self.lines) - 1)
        end_line = clamp(end_line, 0, len(self.lines) - 1)
        self.mode = MODE_VISUAL
        self.visual_anchor = start_line
        self.cy = end_line
        self.cx = clamp(end_col, 0, len(self._line()))
        self._set_message(f"Incremental selection: {label} ({start_line + 1}-{end_line + 1})")

    def _incremental_select_expand(self) -> bool:
        candidates = self._selection_candidates()
        if not candidates:
            self._set_message("No incremental selection candidates.", error=True)
            return False
        if candidates != self._incremental_select_ranges:
            self._incremental_select_ranges = candidates
            self._incremental_select_index = -1
        if self._incremental_select_index + 1 < len(self._incremental_select_ranges):
            self._incremental_select_index += 1
        self._apply_visual_range(self._incremental_select_ranges[self._incremental_select_index])
        return True

    def _incremental_select_shrink(self) -> bool:
        if not self._incremental_select_ranges:
            self._set_message("Incremental selection is empty.", error=True)
            return False
        if self._incremental_select_index <= 0:
            self._set_message("Incremental selection already at smallest range.")
            return True
        self._incremental_select_index -= 1
        self._apply_visual_range(self._incremental_select_ranges[self._incremental_select_index])
        return True

    def _handle_incremental_select_command(self, args: list[str]) -> bool:
        action = args[0].strip().lower() if args else "expand"
        if action in {"expand", "e", "next"}:
            return self._incremental_select_expand()
        if action in {"shrink", "s", "prev"}:
            return self._incremental_select_shrink()
        if action in {"reset", "clear"}:
            self._reset_incremental_selection()
            self.mode = MODE_NORMAL
            self.visual_anchor = None
            self._set_message("Incremental selection reset.")
            return True
        if action in {"list", "ls"}:
            items = self._selection_candidates()
            if not items:
                self._show_alert("(no incremental selection candidates)")
                return True
            lines = [
                f"{index + 1:>2}. {label:<12} {start + 1}:{start_col + 1} -> {end + 1}:{end_col + 1}"
                for index, (start, start_col, end, end_col, label) in enumerate(items)
            ]
            self._show_alert("\n".join(lines))
            return True
        self._set_message("Usage: :iselect expand|shrink|reset|list", error=True)
        return False

    def _set_message(self, message: str, *, error: bool = False) -> None:
        self.message = message
        self.message_error = error
        if self.config.feature_enabled("notifications") and message:
            ttl = 3.0 if error else 2.0
            self._notifications.push(message, ttl_seconds=ttl)

    def _friendly_error_message(self, exc: Exception) -> str:
        details = traceback.TracebackException.from_exception(exc)
        location = ""
        if details.stack:
            frame = details.stack[-1]
            location = f"{Path(frame.filename).name}:{frame.lineno}"
        core = str(exc).strip() or exc.__class__.__name__
        if location:
            return f"Operation failed ({location}): {core}"
        return f"Operation failed: {core}"

    def _capture_snapshot(self) -> ActionSnapshot:
        return ActionSnapshot(
            lines=tuple(self.lines),
            cursor_x=self.cx,
            cursor_y=self.cy,
            line_ending=self._current_line_ending,
        )

    def _push_history_if_changed(self, before: ActionSnapshot, *, label: str) -> None:
        if self._skip_history_once:
            self._skip_history_once = False
            return
        if self._history_suspended or not self._history_enabled:
            return
        after = self._capture_snapshot()
        if before.lines == after.lines:
            return
        self._history.push(ActionRecord(label=label, before=before, after=after))

    def _apply_snapshot(self, snapshot: ActionSnapshot) -> None:
        self._history_suspended = True
        try:
            self.lines = list(snapshot.lines) if snapshot.lines else [""]
            self.cy = clamp(snapshot.cursor_y, 0, len(self.lines) - 1)
            self.cx = clamp(snapshot.cursor_x, 0, len(self._line()))
            self.row_offset = max(0, self.row_offset)
            self.col_offset = max(0, self.col_offset)
            self._current_line_ending = snapshot.line_ending if snapshot.line_ending in {"\n", "\r\n"} else "\n"
            self.buffer.mark_all_dirty()
            self.modified = True
            current_view = self._capture_view_state()
            self._split_main_view = current_view
            self._split_secondary_view = current_view
            self._sync_incremental_syntax(force=True)
            self._reset_incremental_selection()
        finally:
            self._history_suspended = False

    def _undo(self) -> None:
        record = self._history.undo()
        if record is None:
            self._set_message("Nothing to undo.")
            return
        self._skip_history_once = True
        self._apply_snapshot(record.before)
        self.pending_operator = ""
        self.pending_scope = ""
        self._set_message(f"Undo: {record.label}")

    def _redo(self) -> None:
        record = self._history.redo()
        if record is None:
            self._set_message("Nothing to redo.")
            return
        self._skip_history_once = True
        self._apply_snapshot(record.after)
        self.pending_operator = ""
        self.pending_scope = ""
        self._set_message(f"Redo: {record.label}")

    def _undo_branch_prev(self) -> None:
        record = self._history.branch_prev()
        if record is None:
            self._set_message("Undo tree: no previous branch.")
            return
        self._skip_history_once = True
        self._apply_snapshot(record.after)
        self.pending_operator = ""
        self.pending_scope = ""
        self._set_message(f"Undo branch: {record.label}")

    def _undo_branch_next(self) -> None:
        record = self._history.branch_next()
        if record is None:
            self._set_message("Undo tree: no next branch.")
            return
        self._skip_history_once = True
        self._apply_snapshot(record.after)
        self.pending_operator = ""
        self.pending_scope = ""
        self._set_message(f"Undo branch: {record.label}")

    def _undo_tree_lines(self) -> list[str]:
        rows = self._history.view()
        if not rows:
            return ["(undo tree is empty)"]
        lines: list[str] = []
        for row in rows:
            marker = "*" if row.is_current else " "
            parent = "-" if row.parent is None else str(row.parent)
            children = ",".join(str(item) for item in row.children) if row.children else "-"
            lines.append(f"{marker} {row.node_id:>3} <- {parent:>3} -> [{children}] {row.label}")
        return lines

    def _undo_restore(self, node_id: int) -> bool:
        snapshot = self._history.restore(node_id)
        if snapshot is None:
            self._set_message(f"Undo tree node not found: {node_id}", error=True)
            return False
        self._skip_history_once = True
        self._apply_snapshot(snapshot)
        self.pending_operator = ""
        self.pending_scope = ""
        self._pending_motion = ""
        self._set_message(f"Undo tree restored: {node_id}")
        return True

    def _handle_undo_command(self, args: list[str]) -> bool:
        if not args:
            self._undo()
            return True
        action = args[0].strip().lower()
        if action in {"undo", "u", "back"}:
            self._undo()
            return True
        if action in {"redo", "r"}:
            self._redo()
            return True
        if action in {"tree", "list", "ls"}:
            self._show_alert("\n".join(self._undo_tree_lines()))
            return True
        if action in {"restore", "goto", "jump"}:
            if len(args) < 2:
                self._set_message("Usage: :undo restore <node-id>", error=True)
                return False
            try:
                node_id = int(args[1])
            except ValueError:
                self._set_message("Usage: :undo restore <node-id>", error=True)
                return False
            return self._undo_restore(node_id)
        if action in {"branch-prev", "prev"}:
            self._undo_branch_prev()
            return True
        if action in {"branch-next", "next"}:
            self._undo_branch_next()
            return True
        self._set_message("Usage: :undo [undo|redo|tree|restore <node-id>|branch-prev|branch-next]", error=True)
        return False

    def _start_macro_recording(self, register: str) -> None:
        self._macro_recording_register = register.lower()
        self._macro_recording_keys = []
        self._set_message(f"Recording macro @{register.lower()}")

    def _stop_macro_recording(self) -> None:
        register = self._macro_recording_register
        if register is None:
            return
        self._macro_registers[register] = list(self._macro_recording_keys)
        self._macro_recording_register = None
        self._macro_recording_keys = []
        self._save_macro_store(noisy=False)
        self._set_message(f"Recorded macro @{register}")

    def _replay_macro(self, register: str) -> None:
        if not self.config.macros_enabled():
            self._set_message("Macros are disabled in config.", error=True)
            return
        keys = self._macro_registers.get(register, [])
        if not keys:
            self._set_message(f"Macro @{register} is empty.", error=True)
            return
        for key in reversed(keys):
            self._input_queue.appendleft(key)
        self._set_message(f"Replay macro @{register} ({len(keys)} keys)")

    def _record_key_for_macro(self, key: str) -> None:
        if self._macro_recording_register is None or self._macro_replaying:
            return
        if key == "q":
            return
        if self._macro_waiting_action:
            return
        self._macro_recording_keys.append(key)

    def _macro_store_path(self) -> Path:
        return (self._runtime_root / "macros.json").resolve()

    def _save_macro_store(self, *, noisy: bool) -> bool:
        path = self._macro_store_path()
        payload = {
            "registers": {
                register: list(keys)
                for register, keys in sorted(self._macro_registers.items())
                if register and keys
            }
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            if noisy:
                self._set_message(f"Macro save failed: {exc}", error=True)
            return False
        if noisy:
            self._set_message(f"Macros saved: {path}")
        return True

    def _load_macro_store(self, *, noisy: bool) -> bool:
        path = self._macro_store_path()
        if not path.exists():
            if noisy:
                self._set_message(f"Macro store not found: {path}", error=True)
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if noisy:
                self._set_message(f"Macro load failed: {exc}", error=True)
            return False
        if not isinstance(payload, dict):
            if noisy:
                self._set_message("Macro load failed: invalid payload.", error=True)
            return False
        raw_registers = payload.get("registers", payload)
        if not isinstance(raw_registers, dict):
            if noisy:
                self._set_message("Macro load failed: invalid register map.", error=True)
            return False
        loaded: dict[str, list[str]] = {}
        for register, keys in raw_registers.items():
            if not isinstance(register, str):
                continue
            clean = register.strip().lower()
            if len(clean) != 1 or not clean.isalpha():
                continue
            if not isinstance(keys, list):
                continue
            parsed = [item for item in keys if isinstance(item, str)]
            if parsed:
                loaded[clean] = parsed
        self._macro_registers = loaded
        if noisy:
            self._set_message(f"Macros loaded: {len(loaded)} register(s)")
        return True

    def _handle_macro_command(self, args: list[str]) -> bool:
        if not self.config.macros_enabled():
            self._set_message("Macros are disabled in config.", error=True)
            return False
        action = args[0].strip().lower() if args else "list"
        if action in {"list", "ls"}:
            if not self._macro_registers:
                self._show_alert("(no macros)")
                return True
            lines = [
                f"@{register} ({len(keys)} keys)"
                for register, keys in sorted(self._macro_registers.items())
            ]
            self._show_alert("\n".join(lines))
            return True
        if action in {"show", "print"}:
            if len(args) < 2:
                self._set_message("Usage: :macro show <register>", error=True)
                return False
            register = args[1].strip().lower()
            keys = self._macro_registers.get(register, [])
            if not keys:
                self._set_message(f"Macro @{register} is empty.", error=True)
                return False
            self._show_alert(" ".join(keys))
            return True
        if action in {"play", "run"}:
            if len(args) < 2:
                self._set_message("Usage: :macro play <register> [count]", error=True)
                return False
            register = args[1].strip().lower()
            keys = self._macro_registers.get(register, [])
            if not keys:
                self._set_message(f"Macro @{register} is empty.", error=True)
                return False
            count = 1
            if len(args) >= 3:
                try:
                    count = max(1, int(args[2]))
                except ValueError:
                    self._set_message("Usage: :macro play <register> [count]", error=True)
                    return False
            for _ in range(count):
                for key in reversed(keys):
                    self._input_queue.appendleft(key)
            self._set_message(f"Replay macro @{register} x{count}")
            return True
        if action in {"clear", "rm", "remove"}:
            if len(args) < 2 or args[1].strip().lower() in {"all", "*"}:
                self._macro_registers.clear()
                self._save_macro_store(noisy=False)
                self._set_message("Macros cleared: all")
                return True
            register = args[1].strip().lower()
            if register not in self._macro_registers:
                self._set_message(f"Macro @{register} is empty.", error=True)
                return False
            self._macro_registers.pop(register, None)
            self._save_macro_store(noisy=False)
            self._set_message(f"Macro cleared: @{register}")
            return True
        if action == "save":
            return self._save_macro_store(noisy=True)
        if action == "load":
            return self._load_macro_store(noisy=True)
        if action == "record":
            if len(args) < 2:
                self._set_message("Usage: :macro record <register>", error=True)
                return False
            register = args[1].strip().lower()
            if len(register) != 1 or not register.isalpha():
                self._set_message("Macro register must be [a-z].", error=True)
                return False
            if self._macro_recording_register is not None:
                self._set_message("A macro recording is already in progress.", error=True)
                return False
            self._start_macro_recording(register)
            return True
        if action in {"stop", "end"}:
            if self._macro_recording_register is None:
                self._set_message("Macro recording is not active.", error=True)
                return False
            self._stop_macro_recording()
            return True
        self._set_message("Usage: :macro list|show|record|stop|play|clear|save|load", error=True)
        return False

    def _normalize_loaded_text(self, text: str) -> tuple[str, str]:
        if "\r\n" in text:
            line_ending = "\r\n"
        elif "\r" in text:
            line_ending = "\r\n"
        else:
            line_ending = "\n"
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized, line_ending

    def _decode_file_bytes(self, payload: bytes) -> tuple[str, str]:
        last_error: UnicodeDecodeError | None = None
        for encoding in self._encoding_candidates:
            try:
                return payload.decode(encoding), encoding
            except UnicodeDecodeError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return payload.decode("utf-8"), "utf-8"

    def _set_encoding(self, encoding: str) -> bool:
        clean = encoding.strip().lower()
        if not clean:
            self._set_message("Usage: :encoding <name>", error=True)
            return False
        try:
            "".encode(clean)
        except LookupError:
            self._set_message(f"Unknown encoding: {encoding}", error=True)
            return False
        self._current_encoding = clean
        if clean not in self._encoding_candidates:
            self._encoding_candidates.insert(0, clean)
        self._set_message(f"Encoding set: {clean}")
        return True

    def _write_swap_if_needed(self, *, force: bool = False) -> None:
        if not self._swap_enabled or not self.modified or self.file_path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_swap_write < self._swap_interval:
            return
        try:
            self._persistence.write_swap(
                file_path=self.file_path,
                lines=self.lines,
                cursor_x=self.cx,
                cursor_y=self.cy,
                line_ending=self._current_line_ending,
            )
        except OSError as exc:
            self._set_message(f"Swap write failed: {exc}", error=True)
            return
        self._last_swap_write = now

    def _auto_save_if_needed(self) -> None:
        if not self._auto_save_enabled or not self.modified or self.file_path is None:
            return
        now = time.monotonic()
        if now - self._last_auto_save < self._auto_save_interval:
            return
        if self.save_file(quiet=True):
            self._last_auto_save = now

    def _open_swap_prompt(self, path: Path, payload: SwapPayload) -> None:
        self._swap_prompt_path = path
        self._swap_prompt_payload = payload
        self.alert_lines = [
            f"Swap file found for {path}",
            "Press y to recover unsaved content.",
            "Press n to ignore and remove swap file.",
        ]
        self._resume_mode_after_alert = self.mode if self.mode != MODE_ALERT else MODE_NORMAL
        self.mode = MODE_ALERT

    def _maybe_prompt_swap_recovery(self, path: Path) -> None:
        payload = self._persistence.read_swap(path)
        if payload is None:
            return
        self._open_swap_prompt(path, payload)

    def _apply_swap_payload(self) -> None:
        path = self._swap_prompt_path
        payload = self._swap_prompt_payload
        if path is None or payload is None:
            return
        self.lines = payload.lines if payload.lines else [""]
        self.cy = clamp(payload.cursor_y, 0, len(self.lines) - 1)
        self.cx = clamp(payload.cursor_x, 0, len(self._line()))
        self._current_line_ending = payload.line_ending if payload.line_ending in {"\n", "\r\n"} else "\n"
        self.modified = True
        self.buffer.mark_all_dirty()
        self._set_message("Swap recovered.")

    def _clear_swap_prompt(self) -> None:
        self._swap_prompt_path = None
        self._swap_prompt_payload = None
        self._close_alert()

    def _session_payload(self) -> dict[str, object]:
        return {
            "current_file": str(self.file_path) if self.file_path else "",
            "cursor_x": self.cx,
            "cursor_y": self.cy,
            "tabs": list(self._tab_items),
            "tab_index": self._current_tab_index,
            "workspace_root": str(self._workspace_root),
        }

    def _sanitize_session_name(self, name: str) -> str:
        cleaned = "".join(ch for ch in name.strip() if ch.isalnum() or ch in {"_", "-", "."})
        return cleaned.strip(" .")

    def _session_profile_path(self, name: str) -> Path | None:
        clean = self._sanitize_session_name(name)
        if not clean:
            return None
        return (self._session_profiles_dir / f"{clean}.json").resolve()

    def _session_profile_names(self) -> list[str]:
        path = self._session_profiles_dir
        try:
            entries = list(path.glob("*.json"))
        except OSError:
            return []
        names = sorted({entry.stem for entry in entries if entry.is_file() and entry.stem.strip()})
        return names

    def _restore_session_from(self, session_path: Path) -> bool:
        data = self._persistence.load_session(session_path)
        if not data:
            return False
        project_root = data.get("workspace_root")
        if isinstance(project_root, str) and project_root:
            project_path = Path(project_root)
            if project_path.exists() and project_path.is_dir():
                self.open_project(project_path, force=True, startup=True)
        file_name = data.get("current_file")
        if isinstance(file_name, str) and file_name:
            opened = self.open_file(file_name, force=True, startup=True)
            if opened:
                cursor_x = int(data.get("cursor_x", 0))
                cursor_y = int(data.get("cursor_y", 0))
                self.cy = clamp(cursor_y, 0, len(self.lines) - 1)
                self.cx = clamp(cursor_x, 0, len(self._line()))
        tabs = data.get("tabs")
        if isinstance(tabs, list):
            parsed_tabs = [str(item) for item in tabs if str(item).strip()]
            if parsed_tabs:
                self._tab_items = parsed_tabs
        current_index = data.get("tab_index")
        if isinstance(current_index, int):
            self._current_tab_index = clamp(current_index, 0, max(0, len(self._tab_items) - 1))
        return True

    def _restore_session(self) -> None:
        self._restore_session_from(self._session_path)

    def _save_session_to(self, session_path: Path) -> bool:
        if not self._session_enabled:
            return False
        try:
            self._persistence.save_session(session_path, self._session_payload())
        except OSError:
            return False
        return True

    def _save_session(self) -> None:
        self._save_session_to(self._session_path)

    def _start_live_grep_task(self, version: int, query: str) -> None:
        self._live_grep_task_active = True
        self._live_grep_running_version = version

        async def _search() -> tuple[int, str, list[GrepMatch]]:
            matches = await self._live_grep.search(
                self._workspace_root,
                query,
                limit=self.config.live_grep_max_results(),
            )
            return version, query, matches

        self._async_runtime.submit("feature:live-grep", _search())

    def _open_live_grep(self, query: str) -> None:
        if not self.config.live_grep_enabled():
            self._set_message("Live grep is disabled in config.", error=True)
            return
        clean = query.strip()
        if not clean:
            self._set_message("Usage: :grep <query>", error=True)
            return
        self._live_grep_query = clean
        self._live_grep_requested_version += 1
        if not self._live_grep_task_active:
            self._start_live_grep_task(self._live_grep_requested_version, clean)
        self._floating_list = FloatingList(
            title=f'Live Grep "{clean}"',
            footer="<Enter> open  <Esc> close",
            items=["searching..."],
        )
        self._floating_source = "live_grep"
        self.mode = MODE_FLOAT_LIST

    def _accept_live_grep_selection(self) -> None:
        popup = self._floating_list
        if popup is None:
            return
        index = popup.selected
        if index < 0 or index >= len(self._live_grep_matches):
            return
        match = self._live_grep_matches[index]
        self._record_jump_origin()
        if self.open_file(match.file_path, force=False):
            self.cy = clamp(match.line - 1, 0, len(self.lines) - 1)
            self.cx = clamp(match.column - 1, 0, len(self._line()))
            self.mode = MODE_NORMAL
            self._floating_list = None
            self._floating_source = ""
            self._set_message(f"Grep jump: {match.file_path}:{match.line}:{match.column}")

    def _drain_async_events(self, *, max_items: int = 64) -> None:
        for event in self._async_runtime.poll_events(max_items=max_items):
            event_type = event.get("type", "")
            if event_type == "task_done":
                label = str(event.get("label", "task"))
                error = str(event.get("error", "") or "")
                result = event.get("result")
                if label == "feature:file-tree":
                    self._file_tree_task_active = False
                    if not self._file_tree_feature.enabled:
                        continue
                    if error:
                        self._set_message(f"Explorer refresh failed: {error}", error=True)
                        if self._file_tree_requested_version > self._file_tree_running_version:
                            self._start_file_tree_task(self._file_tree_requested_version)
                    else:
                        version = -1
                        paths: list[str] = []
                        if isinstance(result, tuple) and len(result) == 2:
                            version = int(result[0])
                            payload = result[1]
                            if isinstance(payload, list):
                                paths = [str(item) for item in payload]
                        elif isinstance(result, list):
                            paths = [str(item) for item in result]
                            version = self._file_tree_running_version
                        if version != self._file_tree_requested_version:
                            if self._file_tree_requested_version > version:
                                self._start_file_tree_task(self._file_tree_requested_version)
                            continue
                        self._file_tree_feature.apply_paths(paths)
                        if self.mode == MODE_EXPLORER:
                            self._sync_file_tree_popup()
                    continue

                if label == "feature:git-control":
                    self._git_control_task_active = False
                    if not self._git_control_feature.enabled:
                        continue
                    if error:
                        self._set_message(f"Git refresh failed: {error}", error=True)
                        if self._git_control_requested_version > self._git_control_running_version:
                            self._start_git_control_task(self._git_control_requested_version)
                    else:
                        version = -1
                        target_path = self.file_path
                        snapshot: GitSnapshot | None = None
                        if isinstance(result, tuple) and len(result) == 3:
                            version = int(result[0])
                            target_raw = result[1]
                            target_path = Path(target_raw) if isinstance(target_raw, str) and target_raw else None
                            payload = result[2]
                            if isinstance(payload, GitSnapshot):
                                snapshot = payload
                        elif isinstance(result, GitSnapshot):
                            snapshot = result
                            version = self._git_control_running_version
                        stale = version != self._git_control_requested_version or target_path != self.file_path
                        if stale:
                            if self._git_control_requested_version > version:
                                self._start_git_control_task(self._git_control_requested_version)
                            continue
                        if snapshot is not None:
                            self._git_control_feature.apply(snapshot)
                        if self._git_control_requested_version > version:
                            self._start_git_control_task(self._git_control_requested_version)
                    continue

                if label == "feature:live-grep":
                    self._live_grep_task_active = False
                    if error:
                        self._set_message(f"Live grep failed: {error}", error=True)
                        continue
                    version = -1
                    query = self._live_grep_query
                    matches: list[GrepMatch] = []
                    if isinstance(result, tuple) and len(result) == 3:
                        version = int(result[0])
                        query = str(result[1])
                        payload = result[2]
                        if isinstance(payload, list):
                            matches = [item for item in payload if isinstance(item, GrepMatch)]
                    if version != self._live_grep_requested_version:
                        if self._live_grep_requested_version > version:
                            self._start_live_grep_task(self._live_grep_requested_version, self._live_grep_query)
                        continue
                    self._live_grep_query = query
                    self._live_grep_matches = matches
                    self._quickfix_items = [
                        QuickfixItem(
                            path=match.file_path.resolve(),
                            line=max(1, int(match.line)),
                            col=max(1, int(match.column)),
                            text=match.text,
                        )
                        for match in matches[: self.config.quickfix_max_items()]
                    ]
                    self._quickfix_index = 0 if self._quickfix_items else -1
                    if self._floating_source == "live_grep" and self._floating_list is not None:
                        labels = [match.label(self._workspace_root) for match in matches]
                        if not labels:
                            labels = ["(no matches)"]
                        self._floating_list.set_items(labels)
                    self._set_message(f"Live grep: {len(matches)} matches")
                    continue

                if error:
                    self._show_alert(f"Async task failed ({label}): {error}")
                else:
                    self._set_message(f"Async task done: {label}")
                continue

            if event_type == "process_output":
                process_id = int(event.get("process_id", 0))
                line = str(event.get("line", ""))
                if line:
                    session = self._terminal_sessions.get(process_id)
                    if session is not None:
                        session.output.append(line)
                        if len(session.output) > 2000:
                            session.output = session.output[-2000:]
                        if process_id == self._terminal_process_id:
                            self._terminal_output = session.output
                    else:
                        self._set_message(f"[proc:{process_id}] {line}")
                continue

            if event_type == "process_exit":
                process_id = int(event.get("process_id", 0))
                code = event.get("return_code", None)
                session = self._terminal_sessions.get(process_id)
                if session is not None:
                    session.output.append(f"[exit] code={code}")
                    if len(session.output) > 2000:
                        session.output = session.output[-2000:]
                    if process_id == self._terminal_process_id:
                        self._terminal_output = session.output
                        self._set_message(f"Terminal exited ({code}).")
                else:
                    self._set_message(f"Process {process_id} exited ({code}).")
                if process_id == self._dap_session_process_id:
                    self._dap_session_process_id = None
                    self._dap_target_path = None

    def _ast_query(self) -> AstQueryService:
        if self._ast_query_service is None:
            self._ast_query_service = AstQueryService()
        return self._ast_query_service

    def _get_ast_query_service(self) -> AstQueryService | None:
        if self._ast_query_service is None:
            if self._lazy_load_enabled and not (
                self.config.feature_enabled("winbar") or self.config.feature_enabled("tab_completion")
            ):
                return None
            self._ast_query_service = AstQueryService()
        return self._ast_query_service

    def _language_id_for_file(self, path: Path | None) -> str:
        if path is None:
            return "plaintext"
        extension = path.suffix.lower()
        mapping = self.config.lsp_language_id_map()
        return mapping.get(extension, "plaintext")

    def _ensure_lsp_ready(self, *, show_error: bool) -> LspClient | None:
        if not self._lsp_enabled:
            if show_error:
                self._set_message("LSP is disabled in config.", error=True)
            return None
        if not self._lsp_command:
            if show_error:
                self._set_message("LSP command is not configured.", error=True)
            return None
        if self.file_path is None:
            if show_error:
                self._set_message("No file path for LSP.", error=True)
            return None

        if self._lsp_client is None:
            self._lsp_client = LspClient()

        client = self._lsp_client
        language_id = self._language_id_for_file(self.file_path)
        try:
            self._async_runtime.run_sync(
                client.ensure_started(self._lsp_command, self._workspace_root),
                timeout=self._lsp_timeout_seconds,
            )
            self._async_runtime.run_sync(
                client.sync_document(self.file_path, self.lines, language_id),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            if show_error:
                self._set_message(f"LSP unavailable: {exc}", error=True)
            return None
        return client

    def _goto_definition_lsp(self, symbol: str) -> bool:
        client = self._ensure_lsp_ready(show_error=False)
        if client is None or self.file_path is None:
            return False
        try:
            locations = self._async_runtime.run_sync(
                client.definition(self.file_path, self.cy, self.cx),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception:
            return False
        if not locations:
            return False
        target_path, line, col = locations[0]
        self._record_jump_origin()
        if not self.open_file(target_path, force=False):
            return False
        self.cy = clamp(line - 1, 0, len(self.lines) - 1)
        self.cx = clamp(col - 1, 0, len(self._line()))
        self._set_message(f"LSP definition: {symbol}")
        return True

    def _show_hover(self) -> None:
        symbol = word_at_cursor(self._line(), self.cx)
        if not symbol:
            self._set_message("No symbol under cursor.", error=True)
            return
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return
        try:
            text = self._async_runtime.run_sync(
                client.hover(self.file_path, self.cy, self.cx),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP hover failed: {exc}", error=True)
            return
        if not text:
            self._set_message(f"No hover docs: {symbol}", error=True)
            return
        self._show_alert(text[:7000])

    def _show_lsp_diagnostics(self) -> None:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return
        try:
            items = self._async_runtime.run_sync(
                client.diagnostics(self.file_path),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP diagnostics failed: {exc}", error=True)
            return
        if not items:
            self._set_message("No diagnostics.")
            return
        rendered = "\n".join(items[:240])
        self._show_alert(rendered[:7000])

    def _location_label(self, path: Path, line: int, col: int, *, prefix: str = "") -> str:
        try:
            relative = path.resolve().relative_to(self._workspace_root.resolve())
            base = str(relative)
        except ValueError:
            base = str(path)
        core = f"{base}:{max(1, line)}:{max(1, col)}"
        return f"{prefix} {core}".strip()

    def _open_lsp_navigation(
        self,
        title: str,
        locations: list[tuple[Path, int, int]],
        *,
        labels: list[str] | None = None,
    ) -> None:
        if not locations:
            self._set_message(f"{title}: no result.")
            return
        self._lsp_nav_locations = locations
        rendered = labels if labels is not None else [self._location_label(path, line, col) for path, line, col in locations]
        if len(rendered) != len(locations):
            rendered = [self._location_label(path, line, col) for path, line, col in locations]
        self._floating_list = FloatingList(
            title=title,
            footer="<Enter> open  <Esc> close",
            items=rendered,
        )
        self._floating_source = "lsp_nav"
        self._floating_accept_mode = MODE_NORMAL
        self.mode = MODE_FLOAT_LIST

    def _accept_lsp_navigation_selection(self) -> None:
        popup = self._floating_list
        if popup is None:
            return
        index = popup.selected
        if not (0 <= index < len(self._lsp_nav_locations)):
            return
        target_path, line, col = self._lsp_nav_locations[index]
        self._record_jump_origin()
        if self.open_file(target_path, force=False):
            self.cy = clamp(line - 1, 0, len(self.lines) - 1)
            self.cx = clamp(col - 1, 0, len(self._line()))
            self.mode = MODE_NORMAL
            self._floating_list = None
            self._floating_source = ""
            self._set_message(f"LSP jump: {self._location_label(target_path, line, col)}")

    def _show_lsp_references(self) -> None:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return
        try:
            locations = self._async_runtime.run_sync(
                client.references(self.file_path, self.cy, self.cx, include_declaration=False),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP references failed: {exc}", error=True)
            return
        deduped: list[tuple[Path, int, int]] = []
        seen: set[tuple[str, int, int]] = set()
        for path, line, col in locations:
            key = (str(path.resolve()), int(line), int(col))
            if key in seen:
                continue
            seen.add(key)
            deduped.append((path, line, col))
        if not deduped:
            self._set_message("LSP references: no result.")
            return
        self._open_lsp_navigation("LSP References", deduped)

    def _show_lsp_implementation(self) -> None:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return
        try:
            locations = self._async_runtime.run_sync(
                client.implementation(self.file_path, self.cy, self.cx),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP implementation failed: {exc}", error=True)
            return
        if not locations:
            self._set_message("LSP implementation: no result.")
            return
        self._open_lsp_navigation("LSP Implementations", locations)

    def _show_lsp_document_symbols(self, query: str = "") -> None:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return
        try:
            symbols = self._async_runtime.run_sync(
                client.document_symbols(self.file_path),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP symbols failed: {exc}", error=True)
            return
        clean = query.strip().lower()
        if clean:
            symbols = [item for item in symbols if clean in item[0].lower()]
        if not symbols:
            self._set_message("LSP symbols: no result.")
            return
        labels = [self._location_label(path, line, col, prefix=label) for label, path, line, col in symbols]
        locations = [(path, line, col) for _label, path, line, col in symbols]
        self._open_lsp_navigation("LSP Symbols", locations, labels=labels)

    def _show_lsp_workspace_symbols(self, query: str) -> None:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None:
            return
        clean = query.strip() or word_at_cursor(self._line(), self.cx)
        if not clean:
            self._set_message("Usage: :lsp wsymbol <query>", error=True)
            return
        try:
            symbols = self._async_runtime.run_sync(
                client.workspace_symbols(clean),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP workspace symbols failed: {exc}", error=True)
            return
        if not symbols:
            self._set_message(f"LSP workspace symbols: no result for '{clean}'.")
            return
        labels = [self._location_label(path, line, col, prefix=label) for label, path, line, col in symbols]
        locations = [(path, line, col) for _label, path, line, col in symbols]
        self._open_lsp_navigation(f'Workspace Symbols "{clean}"', locations, labels=labels)

    def _lsp_rename_symbol(self, new_name: str) -> bool:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return False
        clean = new_name.strip()
        if not clean:
            self._set_message("Usage: :lsp rename <new_name>", error=True)
            return False
        try:
            payload = self._async_runtime.run_sync(
                client.rename(self.file_path, self.cy, self.cx, clean),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP rename failed: {exc}", error=True)
            return False
        if not payload:
            self._set_message("LSP rename: no changes.")
            return False
        changed = self._apply_workspace_edit(payload)
        self._set_message(f"LSP rename {'applied' if changed else 'returned no editable changes'}.")
        return changed

    def _lsp_format_document(self) -> bool:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return False
        try:
            edits = self._async_runtime.run_sync(
                client.formatting(self.file_path, tab_size=self.tab_size, insert_spaces=True),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP format failed: {exc}", error=True)
            return False
        if not edits:
            self._set_message("LSP format: no changes.")
            return False
        changed = self._apply_text_edits(self.file_path, edits)
        self._set_message("LSP format applied." if changed else "LSP format produced no editable changes.")
        return changed

    def _lsp_uri_to_path(self, uri: str) -> Path | None:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        raw = unquote(parsed.path)
        if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]
        return Path(raw)

    def _apply_edits_to_lines(self, source_lines: list[str], edits: list[dict[str, Any]]) -> tuple[list[str], bool]:
        if not edits:
            return source_lines, False
        lines = list(source_lines) if source_lines else [""]
        sorted_edits = sorted(
            edits,
            key=lambda item: (
                int(item.get("range", {}).get("start", {}).get("line", 0)),
                int(item.get("range", {}).get("start", {}).get("character", 0)),
            ),
            reverse=True,
        )
        changed = False
        for edit in sorted_edits:
            if not isinstance(edit, dict):
                continue
            range_obj = edit.get("range")
            if not isinstance(range_obj, dict):
                continue
            start = range_obj.get("start")
            end = range_obj.get("end")
            if not isinstance(start, dict) or not isinstance(end, dict):
                continue
            try:
                start_line = clamp(int(start.get("line", 0)), 0, len(lines) - 1)
                start_col = max(0, int(start.get("character", 0)))
                end_line = clamp(int(end.get("line", 0)), 0, len(lines) - 1)
                end_col = max(0, int(end.get("character", 0)))
            except (TypeError, ValueError):
                continue
            new_text = str(edit.get("newText", ""))
            if start_line == end_line:
                line = lines[start_line]
                start_col = clamp(start_col, 0, len(line))
                end_col = clamp(end_col, start_col, len(line))
                lines[start_line] = line[:start_col] + new_text + line[end_col:]
            else:
                head = lines[start_line][: clamp(start_col, 0, len(lines[start_line]))]
                tail = lines[end_line][clamp(end_col, 0, len(lines[end_line])) :]
                replacement = (head + new_text + tail).split("\n")
                lines[start_line : end_line + 1] = replacement if replacement else [""]
            changed = True
        return lines, changed

    def _apply_text_edits(self, path: Path, edits: list[dict[str, Any]]) -> bool:
        if not edits:
            return False
        resolved = path.resolve()
        current = self.file_path.resolve() if self.file_path is not None else None
        if current is not None and resolved == current:
            updated, changed = self._apply_edits_to_lines(self.lines, edits)
            if changed:
                self.lines = updated
                self.buffer.mark_all_dirty()
                self._mark_modified()
            return changed

        try:
            raw, encoding = self._decode_file_bytes(resolved.read_bytes())
        except (OSError, UnicodeDecodeError):
            return False
        normalized, line_ending = self._normalize_loaded_text(raw)
        updated, changed = self._apply_edits_to_lines(normalized.split("\n"), edits)
        if not changed:
            return False
        serialized = "\n".join(updated).replace("\n", line_ending)
        try:
            resolved.write_text(serialized, encoding=encoding, newline="")
        except OSError:
            return False
        return True

    def _apply_workspace_edit(self, payload: dict[str, Any]) -> bool:
        changed = False
        changes = payload.get("changes")
        if isinstance(changes, dict):
            for uri, edits in changes.items():
                if not isinstance(uri, str) or not isinstance(edits, list):
                    continue
                path = self._lsp_uri_to_path(uri)
                if path is None:
                    continue
                changed = self._apply_text_edits(path, [item for item in edits if isinstance(item, dict)]) or changed
        doc_changes = payload.get("documentChanges")
        if isinstance(doc_changes, list):
            for item in doc_changes:
                if not isinstance(item, dict):
                    continue
                text_document = item.get("textDocument")
                edits = item.get("edits")
                if not isinstance(text_document, dict) or not isinstance(edits, list):
                    continue
                uri = text_document.get("uri")
                if not isinstance(uri, str):
                    continue
                path = self._lsp_uri_to_path(uri)
                if path is None:
                    continue
                changed = self._apply_text_edits(path, [entry for entry in edits if isinstance(entry, dict)]) or changed
        return changed

    def _open_code_actions(self) -> None:
        client = self._ensure_lsp_ready(show_error=True)
        if client is None or self.file_path is None:
            return
        try:
            diagnostics = self._async_runtime.run_sync(
                client.diagnostics_raw(self.file_path),
                timeout=self._lsp_timeout_seconds,
            )
            actions = self._async_runtime.run_sync(
                client.code_actions(self.file_path, self.cy, self.cx, diagnostics),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception as exc:
            self._set_message(f"LSP code action failed: {exc}", error=True)
            return
        if not actions:
            self._set_message("No code actions.")
            return
        labels: list[str] = []
        filtered: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            title = action.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            filtered.append(action)
            labels.append(title.strip())
        if not filtered:
            self._set_message("No code actions.")
            return
        self._lsp_code_actions = filtered
        self._floating_list = FloatingList(
            title="Code Actions",
            footer="<Enter> apply  <Esc> close",
            items=labels,
        )
        self._floating_source = "code_action"
        self._floating_accept_mode = MODE_NORMAL
        self.mode = MODE_FLOAT_LIST

    def _accept_code_action_selection(self) -> None:
        popup = self._floating_list
        if popup is None:
            return
        index = popup.selected
        if not (0 <= index < len(self._lsp_code_actions)):
            return
        action = self._lsp_code_actions[index]
        client = self._ensure_lsp_ready(show_error=True)
        if client is None:
            return
        applied = False
        edit = action.get("edit")
        if isinstance(edit, dict):
            applied = self._apply_workspace_edit(edit) or applied
        command = action.get("command")
        command_name: str | None = None
        arguments: list[Any] = []
        if isinstance(command, dict):
            raw_name = command.get("command")
            if isinstance(raw_name, str):
                command_name = raw_name
            raw_args = command.get("arguments")
            if isinstance(raw_args, list):
                arguments = list(raw_args)
        elif isinstance(command, str):
            command_name = command
        if command_name:
            try:
                result = self._async_runtime.run_sync(
                    client.execute_command(command_name, arguments),
                    timeout=self._lsp_timeout_seconds,
                )
            except Exception as exc:
                self._set_message(f"LSP executeCommand failed: {exc}", error=True)
                return
            if isinstance(result, dict):
                applied = self._apply_workspace_edit(result) or applied
        self._floating_list = None
        self._floating_source = ""
        self.mode = MODE_NORMAL
        self._set_message("Code action applied." if applied else "Code action executed.")

    def _lsp_completion_candidates(self) -> list[str]:
        client = self._ensure_lsp_ready(show_error=False)
        if client is None or self.file_path is None:
            return []
        try:
            items = self._async_runtime.run_sync(
                client.completion(self.file_path, self.cy, self.cx),
                timeout=self._lsp_timeout_seconds,
            )
        except Exception:
            return []
        return [item for item in items if item.strip()]

    def _syntax_manager(self) -> SyntaxManager:
        if self.syntax is None:
            self.syntax = SyntaxManager(self.config)
            self._syntax_profile = self.syntax.profile_for_file(self.file_path)
        return self.syntax

    def _start_file_tree_task(self, version: int) -> None:
        self._file_tree_task_active = True
        self._file_tree_running_version = version

        async def _collect() -> tuple[int, list[str]]:
            paths = await self._file_tree_feature.collect_paths(self._workspace_root)
            return version, paths

        self._async_runtime.submit("feature:file-tree", _collect())

    def _schedule_file_tree_refresh(self) -> None:
        if not self._file_tree_feature.enabled:
            return
        self._file_tree_requested_version += 1
        if self._file_tree_task_active:
            return
        self._start_file_tree_task(self._file_tree_requested_version)

    def _start_git_control_task(self, version: int) -> None:
        target = self.file_path
        self._git_control_task_active = True
        self._git_control_running_version = version

        async def _collect() -> tuple[int, str, GitSnapshot]:
            snapshot = await self._git_control_feature.collect(self._workspace_root, target)
            target_str = str(target) if target is not None else ""
            return version, target_str, snapshot

        self._async_runtime.submit("feature:git-control", _collect())

    def _schedule_git_control_refresh(self, *, force: bool = False) -> None:
        if not self._git_control_feature.enabled or self.file_path is None:
            return
        now = time.monotonic()
        refresh_interval = max(0.3, self.config.git_refresh_seconds())
        if not force and now - self._git_control_last_refresh < refresh_interval:
            return
        self._git_control_last_refresh = now
        self._git_control_requested_version += 1
        if self._git_control_task_active:
            return
        self._start_git_control_task(self._git_control_requested_version)

    def _ensure_plugins_loaded(self) -> None:
        if not self.config.feature_enabled("plugins"):
            return
        if self._plugins_loaded:
            return
        results = self.plugins.load_all()
        self._plugins_loaded = True
        for result in results:
            if "error" in result.lower():
                self._show_alert(result)
                break

    def _detect_workspace_root(self, anchor: Path | None) -> Path:
        base = anchor if anchor is not None else self._workspace_root
        start = base if base.is_dir() else base.parent
        start = start.resolve()
        markers = (
            ".git",
            ".pvim.project.json",
            "pyproject.toml",
            "package.json",
            "requirements.txt",
            "setup.py",
            "Cargo.toml",
            "go.mod",
            ".hg",
        )
        for candidate in (start, *start.parents):
            if any((candidate / marker).exists() for marker in markers):
                return candidate
        return start

    def _resolve_path(self, target: Path | str) -> Path:
        path = Path(target).expanduser()
        if path.is_absolute():
            return path

        base = self.file_path.parent if self.file_path else self._workspace_root
        return (base / path).resolve()

    def _terminal_size(self) -> tuple[int, int]:
        return self._ui.get_size()

    def _line(self) -> str:
        return self.lines[self.cy]

    def _set_line(self, value: str) -> None:
        self.lines[self.cy] = value
        self.buffer.mark_dirty(self.cy)

    def _mark_modified(self) -> None:
        self.modified = True
        self.pending_operator = ""
        self.buffer.mark_dirty(self.cy)
        self._sync_incremental_syntax()
        self._reset_incremental_selection()

    def _target_edit_rows(self) -> list[int]:
        rows = {self.cy}
        for row in self.extra_cursor_lines:
            if 0 <= row < len(self.lines):
                rows.add(row)
        return sorted(rows)

    def _clear_multi_cursor(self) -> None:
        self.extra_cursor_lines = []

    def _ensure_cursor_bounds(self) -> None:
        self.cy = clamp(self.cy, 0, len(self.lines) - 1)
        self.cx = clamp(self.cx, 0, len(self._line()))

    def _capture_view_state(self) -> tuple[int, int, int, int]:
        return (self.cx, self.cy, self.row_offset, self.col_offset)

    def _apply_view_state(self, state: tuple[int, int, int, int]) -> None:
        cx, cy, row_offset, col_offset = state
        self.cy = clamp(cy, 0, len(self.lines) - 1)
        self.cx = clamp(cx, 0, len(self._line()))
        self.row_offset = max(0, row_offset)
        self.col_offset = max(0, col_offset)

    def _capture_jump_location(self) -> tuple[str, int, int, int, int]:
        path = str(self.file_path.resolve()) if self.file_path is not None else ""
        return (path, int(self.cy), int(self.cx), int(self.row_offset), int(self.col_offset))

    def _restore_jump_location(self, location: tuple[str, int, int, int, int]) -> bool:
        path_text, row, col, row_offset, col_offset = location
        target_path = Path(path_text) if path_text else None
        if target_path is not None:
            current = self.file_path.resolve() if self.file_path is not None else None
            if current != target_path.resolve():
                if not self.open_file(target_path, force=False):
                    return False
        self.cy = clamp(row, 0, len(self.lines) - 1)
        self.cx = clamp(col, 0, len(self._line()))
        self.row_offset = max(0, row_offset)
        self.col_offset = max(0, col_offset)
        return True

    def _record_jump_origin(self) -> None:
        current = self._capture_jump_location()
        if self._jump_back_stack and self._jump_back_stack[-1] == current:
            return
        self._jump_back_stack.append(current)
        if len(self._jump_back_stack) > self._jump_history_limit:
            self._jump_back_stack = self._jump_back_stack[-self._jump_history_limit :]
        self._jump_forward_stack = []

    def _jump_back(self) -> bool:
        if not self._jump_back_stack:
            self._set_message("Jump history: no previous location.")
            return False
        target = self._jump_back_stack.pop()
        self._jump_forward_stack.append(self._capture_jump_location())
        if not self._restore_jump_location(target):
            self._jump_forward_stack.pop()
            self._set_message("Jump history back failed.", error=True)
            return False
        self._set_message("Jumped back.")
        return True

    def _jump_forward(self) -> bool:
        if not self._jump_forward_stack:
            self._set_message("Jump history: no forward location.")
            return False
        target = self._jump_forward_stack.pop()
        self._jump_back_stack.append(self._capture_jump_location())
        if len(self._jump_back_stack) > self._jump_history_limit:
            self._jump_back_stack = self._jump_back_stack[-self._jump_history_limit :]
        if not self._restore_jump_location(target):
            self._jump_back_stack.pop()
            self._set_message("Jump history forward failed.", error=True)
            return False
        self._set_message("Jumped forward.")
        return True

    def _jump_list_lines(self) -> list[str]:
        if not self._jump_back_stack:
            return ["(empty)"]
        lines: list[str] = []
        for index, entry in enumerate(reversed(self._jump_back_stack[-20:]), start=1):
            path_text, row, col, _row_offset, _col_offset = entry
            if path_text:
                path = Path(path_text)
                try:
                    relative = path.resolve().relative_to(self._workspace_root.resolve())
                    label = str(relative)
                except ValueError:
                    label = str(path)
            else:
                label = "[No Name]"
            lines.append(f"{index:>2}. {label}:{row + 1}:{col + 1}")
        return lines

    def _capture_split_active_view(self) -> None:
        if not self._split_enabled:
            return
        current = self._capture_view_state()
        if self._split_focus == "main":
            self._split_main_view = current
        else:
            self._split_secondary_view = current

    def _open_split(self, orientation: str) -> None:
        if orientation not in {"vertical", "horizontal"}:
            self._set_message(f"Unknown split orientation: {orientation}", error=True)
            return
        self._capture_split_active_view()
        current = self._capture_view_state()
        self._split_enabled = True
        self._split_orientation = orientation
        self._split_focus = "main"
        self._split_main_view = current
        self._split_secondary_view = current
        self._set_message(f"Split opened ({orientation}).")

    def _close_split(self) -> None:
        if not self._split_enabled:
            self._set_message("Split is not active.")
            return
        self._capture_split_active_view()
        self._split_enabled = False
        self._split_focus = "main"
        self._set_message("Split closed.")

    def _toggle_split_focus(self) -> None:
        if not self._split_enabled:
            self._set_message("Split is not active.", error=True)
            return
        self._capture_split_active_view()
        if self._split_focus == "main":
            self._split_focus = "secondary"
            self._apply_view_state(self._split_secondary_view)
        else:
            self._split_focus = "main"
            self._apply_view_state(self._split_main_view)
        self._set_message(f"Split focus: {self._split_focus}")

    def _resize_split(self, delta: float) -> None:
        if not self._split_enabled:
            self._set_message("Split is not active.", error=True)
            return
        self._split_ratio = max(0.25, min(0.75, self._split_ratio + delta))
        self._set_message(f"Split ratio: {self._split_ratio:.2f}")

    def _split_vertical_sizes(self, editor_width: int) -> tuple[int, int] | None:
        usable = max(1, editor_width - 1)
        min_pane = 12
        if usable < min_pane * 2:
            return None
        left = clamp(int(usable * self._split_ratio), min_pane, usable - min_pane)
        right = usable - left
        return left, right

    def _active_sidebar_width(self, width: int) -> int:
        if not self.show_sidebar or not self.config.sidebar_enabled():
            return 0
        if self.file_path is not None and not self._project_mode and not self._sidebar_manual_override:
            return 0
        if self.mode in {MODE_FUZZY, MODE_FLOAT_LIST, MODE_EXPLORER, MODE_COMPLETION, MODE_KEY_HINTS, MODE_ALERT, MODE_TERMINAL}:
            return 0
        preferred = self.sidebar_width
        if width - preferred < 20:
            return max(0, width - 20)
        return preferred

    def _gutter_width(self) -> int:
        if not self.show_line_numbers or self.mode in {
            MODE_FUZZY,
            MODE_FLOAT_LIST,
            MODE_EXPLORER,
            MODE_COMPLETION,
            MODE_KEY_HINTS,
            MODE_ALERT,
            MODE_TERMINAL,
        }:
            return 0
        return max(3, len(str(max(1, len(self.lines))))) + 1

    def _ensure_cursor_visible(self, width: int, height: int, sidebar_width: int) -> None:
        text_rows = max(1, height - 2)
        gutter = self._gutter_width()
        text_cols = max(1, width - sidebar_width - gutter)

        if self.cy < self.row_offset:
            self.row_offset = self.cy
        elif self.cy >= self.row_offset + text_rows:
            self.row_offset = self.cy - text_rows + 1

        if self._soft_wrap_enabled:
            self.col_offset = 0
            while self.row_offset < self.cy and self._cursor_softwrap_row(text_cols) > text_rows:
                self.row_offset += 1
            while self.row_offset > 0:
                previous = self.row_offset - 1
                probe = self.row_offset
                self.row_offset = previous
                if self._cursor_softwrap_row(text_cols) <= text_rows:
                    continue
                self.row_offset = probe
                break
        else:
            line = self._line()
            cursor_display = display_width(line[: self.cx])
            if cursor_display < self.col_offset:
                self.col_offset = cursor_display
            elif cursor_display >= self.col_offset + text_cols:
                self.col_offset = cursor_display - text_cols + 1

        self.row_offset = max(0, self.row_offset)
        self.col_offset = max(0, self.col_offset)

    def _move_left(self) -> None:
        if self.cx > 0:
            self.cx -= 1
            return
        if self.cy > 0:
            self.cy -= 1
            self.cx = len(self._line())

    def _move_right(self) -> None:
        line_len = len(self._line())
        if self.cx < line_len:
            self.cx += 1
            return
        if self.cy < len(self.lines) - 1:
            self.cy += 1
            self.cx = 0

    def _move_up(self) -> None:
        if self.cy > 0:
            self.cy -= 1
            self.cx = min(self.cx, len(self._line()))

    def _move_down(self) -> None:
        if self.cy < len(self.lines) - 1:
            self.cy += 1
            self.cx = min(self.cx, len(self._line()))

    def _bracket_probe(self, row: int, col: int) -> tuple[int, int, str] | None:
        if not (0 <= row < len(self.lines)):
            return None
        line = self.lines[row]
        pairs = {"(", ")", "[", "]", "{", "}"}
        probe_col = col
        token = line[probe_col] if 0 <= probe_col < len(line) else ""
        if token not in pairs and probe_col > 0:
            probe_col -= 1
            token = line[probe_col] if 0 <= probe_col < len(line) else ""
        if token not in pairs:
            return None
        return row, probe_col, token

    def _find_matching_bracket(
        self,
        row: int,
        col: int,
    ) -> tuple[int, int, int, int, bool] | None:
        probe = self._bracket_probe(row, col)
        if probe is None:
            return None
        probe_row, probe_col, token = probe
        pairs = {
            "(": ")",
            "[": "]",
            "{": "}",
            ")": "(",
            "]": "[",
            "}": "{",
        }
        opening = token in {"(", "[", "{"}
        open_ch = token if opening else pairs[token]
        close_ch = pairs[token] if opening else token
        depth = 0

        if opening:
            for scan_row in range(probe_row, len(self.lines)):
                start_col = probe_col + 1 if scan_row == probe_row else 0
                text = self.lines[scan_row]
                for scan_col in range(start_col, len(text)):
                    current = text[scan_col]
                    if current == open_ch:
                        depth += 1
                    elif current == close_ch:
                        if depth == 0:
                            return probe_row, probe_col, scan_row, scan_col, True
                        depth -= 1
            return None

        for scan_row in range(probe_row, -1, -1):
            text = self.lines[scan_row]
            start_col = probe_col - 1 if scan_row == probe_row else len(text) - 1
            for scan_col in range(start_col, -1, -1):
                current = text[scan_col]
                if current == close_ch:
                    depth += 1
                elif current == open_ch:
                    if depth == 0:
                        return probe_row, probe_col, scan_row, scan_col, False
                    depth -= 1
        return None

    def _active_bracket_pair_under_cursor(self) -> tuple[int, int, int, int] | None:
        match = self._find_matching_bracket(self.cy, self.cx)
        if match is None:
            return None
        probe_row, probe_col, target_row, target_col, opening = match
        if opening:
            return probe_row, probe_col, target_row, target_col
        return target_row, target_col, probe_row, probe_col

    def _jump_to_matching_bracket(self) -> bool:
        if self._bracket_probe(self.cy, self.cx) is None:
            self._set_message("No bracket under cursor.", error=True)
            return False
        match = self._find_matching_bracket(self.cy, self.cx)
        if match is None:
            self._set_message("Matching bracket not found.", error=True)
            return False
        _probe_row, _probe_col, target_row, target_col, _opening = match
        self._record_jump_origin()
        self.cy = target_row
        self.cx = target_col
        self._set_message("Matched bracket.")
        return True

    def _line_bracket_style_map(self, line_index: int, text: str) -> dict[int, str]:
        if not text:
            return {}
        style_map: dict[int, str] = {}
        open_tokens = {"(", "[", "{"}
        close_tokens = {")", "]", "}"}
        levels = max(1, len(self._bracket_styles))
        depth = max(0, self._syntax_model.depth_before_line(line_index))
        for index, token in enumerate(text):
            if token in open_tokens:
                style_index = depth % levels
                if self._bracket_styles:
                    style_map[index] = self._bracket_styles[style_index]
                depth += 1
                continue
            if token in close_tokens:
                depth = max(0, depth - 1)
                style_index = depth % levels
                if self._bracket_styles:
                    style_map[index] = self._bracket_styles[style_index]

        active = self._active_bracket_pair
        if active is not None and self._bracket_active_style:
            open_row, open_col, close_row, close_col = active
            if line_index == open_row and 0 <= open_col < len(text):
                style_map[open_col] = self._bracket_active_style
            if line_index == close_row and 0 <= close_col < len(text):
                style_map[close_col] = self._bracket_active_style
        return style_map

    def _visible_bracket_style_map(
        self,
        line_index: int,
        line_text: str,
        start_display: int,
        visible_text: str,
    ) -> dict[int, str]:
        if not visible_text:
            return {}
        full_map = self._line_bracket_style_map(line_index, line_text)
        if not full_map:
            return {}
        start_index = index_from_display_col(line_text, start_display)
        visible_map: dict[int, str] = {}
        for local_index in range(len(visible_text)):
            style = full_map.get(start_index + local_index)
            if style:
                visible_map[local_index] = style
        return visible_map

    def _active_pair_line_markers(self) -> set[int]:
        active = self._active_bracket_pair
        if active is None:
            return set()
        open_row, _open_col, close_row, _close_col = active
        if open_row == close_row:
            return {open_row}
        return {open_row, close_row}

    def _highlight_code_with_search(
        self,
        text: str,
        base_style: str,
        query: str,
        *,
        line_index: int | None = None,
        line_text: str = "",
        start_display: int = 0,
    ) -> str:
        if not text:
            return base_style
        syntax = self._syntax_manager()
        overlays: list[str | None] = [None] * len(text)
        if query:
            match_style = self.theme.ui_style("selection") or base_style
            for match in re.finditer(re.escape(query), text):
                start, end = match.span()
                for index in range(start, end):
                    overlays[index] = match_style
        if line_index is not None:
            bracket_styles = self._visible_bracket_style_map(
                line_index,
                line_text,
                start_display,
                text,
            )
            for index, style in bracket_styles.items():
                if 0 <= index < len(overlays):
                    overlays[index] = style
        if all(item is None for item in overlays):
            return syntax.highlight_line(text, self._syntax_profile, self.theme, base_style)
        out: list[str] = []
        cursor = 0
        length = len(text)
        while cursor < length:
            style = overlays[cursor]
            end = cursor + 1
            while end < length and overlays[end] == style:
                end += 1
            segment = text[cursor:end]
            if style is None:
                out.append(syntax.highlight_line(segment, self._syntax_profile, self.theme, base_style))
            else:
                out.append(f"{style}{segment}{base_style}")
            cursor = end
        return "".join(out)

    def _move_word_left(self) -> None:
        if self.cx == 0 and self.cy > 0:
            self.cy -= 1
            self.cx = len(self._line())

        line = self._line()
        index = self.cx
        while index > 0 and line[index - 1].isspace():
            index -= 1
        while index > 0 and is_word_char(line[index - 1]):
            index -= 1
        if index == self.cx and index > 0:
            index -= 1
        self.cx = index

    def _move_word_right(self) -> None:
        line = self._line()
        index = self.cx
        length = len(line)
        while index < length and line[index].isspace():
            index += 1
        while index < length and is_word_char(line[index]):
            index += 1

        if index >= length and self.cy < len(self.lines) - 1:
            self.cy += 1
            self.cx = 0
            return
        self.cx = index

    def _page_step(self) -> int:
        _, height = self._terminal_size()
        return max(1, height - 2)

    def _page_up(self) -> None:
        self.cy = max(0, self.cy - self._page_step())
        self.cx = min(self.cx, len(self._line()))

    def _page_down(self) -> None:
        self.cy = min(len(self.lines) - 1, self.cy + self._page_step())
        self.cx = min(self.cx, len(self._line()))

    def _insert_text_multi(self, text: str) -> None:
        rows = self._target_edit_rows()
        for row in rows:
            line = self.lines[row]
            column = min(self.cx, len(line))
            self.lines[row] = line[:column] + text + line[column:]
            self.buffer.mark_dirty(row)
        self.cx += len(text)
        self._mark_modified()

    def _insert_newline(self) -> None:
        if self.extra_cursor_lines:
            self._clear_multi_cursor()
        line = self._line()
        left = line[: self.cx]
        right = line[self.cx :]
        indent = left[: len(left) - len(left.lstrip(" \t"))]
        trigger = left.rstrip()
        if trigger.endswith(("{", "[", "(")) or trigger.endswith(":"):
            indent = indent + (" " * self.tab_size)
        right_trimmed = right.lstrip(" \t") if right.startswith((" ", "\t")) else right
        self._set_line(left)
        self.lines.insert(self.cy + 1, indent + right_trimmed)
        self.buffer.mark_all_dirty()
        self.cy += 1
        self.cx = len(indent)
        self._mark_modified()

    def _handle_pair_backspace(self) -> bool:
        if not self._auto_pairs:
            return False
        if self.extra_cursor_lines:
            return False
        if self.cx <= 0:
            return False

        line = self._line()
        if self.cx >= len(line):
            return False

        left_char = line[self.cx - 1]
        right_char = line[self.cx]
        paired = self._auto_pairs.get(left_char)
        if paired != right_char:
            return False

        self._set_line(line[: self.cx - 1] + line[self.cx + 1 :])
        self.cx -= 1
        self._mark_modified()
        return True

    def _backspace(self) -> None:
        if self._handle_pair_backspace():
            return

        if self.extra_cursor_lines and self.cx > 0:
            rows = self._target_edit_rows()
            for row in rows:
                line = self.lines[row]
                column = min(self.cx, len(line))
                if column > 0:
                    self.lines[row] = line[: column - 1] + line[column:]
                    self.buffer.mark_dirty(row)
            self.cx -= 1
            self._mark_modified()
            return

        if self.cx > 0:
            line = self._line()
            self._set_line(line[: self.cx - 1] + line[self.cx :])
            self.cx -= 1
            self._mark_modified()
            return

        if self.cy == 0:
            return

        self._clear_multi_cursor()
        prev = self.lines[self.cy - 1]
        current = self.lines.pop(self.cy)
        self.cy -= 1
        self.cx = len(prev)
        self.lines[self.cy] = prev + current
        self.buffer.mark_all_dirty()
        self._mark_modified()

    def _delete_char(self) -> None:
        if self.extra_cursor_lines:
            changed = False
            for row in self._target_edit_rows():
                line = self.lines[row]
                column = min(self.cx, len(line))
                if column < len(line):
                    self.lines[row] = line[:column] + line[column + 1 :]
                    self.buffer.mark_dirty(row)
                    changed = True
            if changed:
                self._mark_modified()
            return

        line = self._line()
        if self.cx < len(line):
            self._set_line(line[: self.cx] + line[self.cx + 1 :])
            self._mark_modified()
            return

        if self.cy < len(self.lines) - 1:
            self.lines[self.cy] = line + self.lines.pop(self.cy + 1)
            self.buffer.mark_all_dirty()
            self._mark_modified()

    def _delete_line(self) -> None:
        self._clear_multi_cursor()
        if len(self.lines) == 1:
            self.lines[0] = ""
            self.cx = 0
            self._mark_modified()
            return

        self.lines.pop(self.cy)
        self.buffer.mark_all_dirty()
        self.cy = min(self.cy, len(self.lines) - 1)
        self.cx = min(self.cx, len(self._line()))
        self._mark_modified()

    def _open_line_below(self) -> None:
        self._clear_multi_cursor()
        source = self._line()
        indent = source[: len(source) - len(source.lstrip(" \t"))]
        if source.rstrip().endswith(("{", "[", "(")) or source.rstrip().endswith(":"):
            indent = indent + (" " * self.tab_size)
        self.cy += 1
        self.lines.insert(self.cy, indent)
        self.buffer.mark_all_dirty()
        self.cx = len(indent)
        self.mode = MODE_INSERT
        self._mark_modified()
        self._set_message("-- INSERT --")

    def _open_line_above(self) -> None:
        self._clear_multi_cursor()
        source = self._line()
        indent = source[: len(source) - len(source.lstrip(" \t"))]
        self.lines.insert(self.cy, indent)
        self.buffer.mark_all_dirty()
        self.cx = len(indent)
        self.mode = MODE_INSERT
        self._mark_modified()
        self._set_message("-- INSERT --")

    def _line_is_selected(self, line_index: int) -> bool:
        if self.mode != MODE_VISUAL or self.visual_anchor is None:
            return False
        start = min(self.visual_anchor, self.cy)
        end = max(self.visual_anchor, self.cy)
        return start <= line_index <= end

    def _selected_line_range(self) -> tuple[int, int] | None:
        if self.mode != MODE_VISUAL or self.visual_anchor is None:
            return None
        return min(self.visual_anchor, self.cy), max(self.visual_anchor, self.cy)

    def _paragraph_range(self, scope: str) -> tuple[int, int] | None:
        if not self.lines:
            return None
        anchor = self.cy
        if not self.lines[anchor].strip():
            probe = anchor
            while probe < len(self.lines) and not self.lines[probe].strip():
                probe += 1
            if probe >= len(self.lines):
                probe = anchor
                while probe >= 0 and not self.lines[probe].strip():
                    probe -= 1
            if probe < 0 or probe >= len(self.lines):
                return None
            anchor = probe

        start = anchor
        end = anchor
        while start > 0 and self.lines[start - 1].strip():
            start -= 1
        while end < len(self.lines) - 1 and self.lines[end + 1].strip():
            end += 1
        if scope == "a":
            if start > 0 and not self.lines[start - 1].strip():
                start -= 1
            if end < len(self.lines) - 1 and not self.lines[end + 1].strip():
                end += 1
        return start, end

    def _ast_text_object_range(self, kind: str, scope: str) -> tuple[int, int, int, int] | None:
        if self.file_path is None:
            return None
        source = "\n".join(self.lines)
        match = self._ast_query().query_at(
            file_path=self.file_path,
            source=source,
            row=self.cy + 1,
            col=self.cx + 1,
            kinds={kind},
        )
        if match is None:
            return None
        start_line = clamp(match.start_line - 1, 0, len(self.lines) - 1)
        end_line = clamp(match.end_line - 1, 0, len(self.lines) - 1)
        start_col = max(0, match.start_col - 1)
        end_col = max(0, match.end_col - 1)
        if scope == "i" and end_line - start_line >= 1:
            start_line += 1
            start_col = 0
            end_col = len(self.lines[end_line])
        return start_line, start_col, end_line, end_col

    def _delete_char_range(self, start_line: int, start_col: int, end_line: int, end_col: int) -> None:
        if (start_line, start_col) > (end_line, end_col):
            start_line, end_line = end_line, start_line
            start_col, end_col = end_col, start_col

        start_line = clamp(start_line, 0, len(self.lines) - 1)
        end_line = clamp(end_line, 0, len(self.lines) - 1)
        start_col = clamp(start_col, 0, len(self.lines[start_line]))
        end_col = clamp(end_col, 0, len(self.lines[end_line]))

        if start_line == end_line:
            line = self.lines[start_line]
            self.lines[start_line] = line[:start_col] + line[end_col:]
        else:
            head = self.lines[start_line][:start_col]
            tail = self.lines[end_line][end_col:]
            self.lines[start_line : end_line + 1] = [head + tail]
        if not self.lines:
            self.lines = [""]
        self.cy = clamp(start_line, 0, len(self.lines) - 1)
        self.cx = clamp(start_col, 0, len(self._line()))
        self.buffer.mark_all_dirty()
        self._mark_modified()

    def _delete_line_range(self, start_line: int, end_line: int) -> None:
        if start_line > end_line:
            start_line, end_line = end_line, start_line
        start_line = clamp(start_line, 0, len(self.lines) - 1)
        end_line = clamp(end_line, 0, len(self.lines) - 1)
        del self.lines[start_line : end_line + 1]
        if not self.lines:
            self.lines = [""]
        self.cy = clamp(start_line, 0, len(self.lines) - 1)
        self.cx = clamp(self.cx, 0, len(self._line()))
        self.buffer.mark_all_dirty()
        self._mark_modified()

    def _apply_text_object(self, operator: str, scope: str, key: str) -> bool:
        if not self.config.text_objects_enabled():
            self._set_message("Text objects are disabled in config.", error=True)
            return True

        if key == "w":
            word = word_range(self._line(), self.cx, scope)
            if word is None:
                self._set_message("No word text object.", error=True)
                return True
            if operator == "v":
                self.mode = MODE_VISUAL
                self.visual_anchor = self.cy
                return True
            self._delete_char_range(self.cy, word[0], self.cy, word[1])
            if operator == "c":
                self.mode = MODE_INSERT
                self._set_message("-- INSERT --")
            return True

        if key == '"':
            quote = quote_range(self._line(), self.cx, '"', scope)
            if quote is None:
                self._set_message('No quote text object for ".', error=True)
                return True
            if operator == "v":
                self.mode = MODE_VISUAL
                self.visual_anchor = self.cy
                return True
            self._delete_char_range(self.cy, quote[0], self.cy, quote[1])
            if operator == "c":
                self.mode = MODE_INSERT
                self._set_message("-- INSERT --")
            return True

        if key == "p":
            paragraph = self._paragraph_range(scope)
            if paragraph is None:
                self._set_message("No paragraph text object.", error=True)
                return True
            if operator == "v":
                self.mode = MODE_VISUAL
                self.visual_anchor = paragraph[0]
                self.cy = paragraph[1]
                self.cx = 0
                return True
            self._delete_line_range(paragraph[0], paragraph[1])
            if operator == "c":
                self.mode = MODE_INSERT
                self._set_message("-- INSERT --")
            return True

        if key in {"f", "c"}:
            kind = "function" if key == "f" else "class"
            match = self._ast_text_object_range(kind, scope)
            if match is None:
                self._set_message(f"No AST {kind} text object.", error=True)
                return True
            if operator == "v":
                self.mode = MODE_VISUAL
                self.visual_anchor = match[0]
                self.cy = match[2]
                self.cx = 0
                return True
            if scope == "a":
                self._delete_line_range(match[0], match[2])
            else:
                self._delete_char_range(match[0], match[1], match[2], match[3])
            if operator == "c":
                self.mode = MODE_INSERT
                self._set_message("-- INSERT --")
            return True

        if key in {"b", "(", "[", "{"}:
            bracket = self._bracket_text_object_range(scope, key)
            if bracket is None:
                self._set_message("No bracket text object.", error=True)
                return True
            if operator == "v":
                self.mode = MODE_VISUAL
                self.visual_anchor = bracket[0]
                self.cy = bracket[2]
                self.cx = clamp(bracket[3], 0, len(self._line()))
                return True
            self._delete_char_range(bracket[0], bracket[1], bracket[2], bracket[3])
            if operator == "c":
                self.mode = MODE_INSERT
                self._set_message("-- INSERT --")
            return True

        return False

    def _bracket_text_object_range(self, scope: str, key: str) -> tuple[int, int, int, int] | None:
        expected_open = key if key in {"(", "[", "{"} else ""

        def _normalize(match: tuple[int, int, int, int, bool]) -> tuple[int, int, int, int]:
            probe_row, probe_col, target_row, target_col, opening = match
            if opening:
                return probe_row, probe_col, target_row, target_col
            return target_row, target_col, probe_row, probe_col

        def _contains_cursor(start_row: int, start_col: int, end_row: int, end_col: int) -> bool:
            if self.cy < start_row or self.cy > end_row:
                return False
            if self.cy == start_row and self.cx < start_col:
                return False
            if self.cy == end_row and self.cx > end_col:
                return False
            return True

        candidates: list[tuple[int, int, int, int]] = []

        direct = self._find_matching_bracket(self.cy, self.cx)
        if direct is not None:
            candidates.append(_normalize(direct))

        for row in range(self.cy, -1, -1):
            text = self.lines[row]
            if not text:
                continue
            start_col = self.cx if row == self.cy else len(text) - 1
            start_col = clamp(start_col, 0, len(text) - 1)
            for col in range(start_col, -1, -1):
                token = text[col]
                if token not in {"(", "[", "{"}:
                    continue
                if expected_open and token != expected_open:
                    continue
                maybe = self._find_matching_bracket(row, col)
                if maybe is None:
                    continue
                normalized = _normalize(maybe)
                if _contains_cursor(*normalized):
                    candidates.append(normalized)

        best: tuple[int, int, int, int] | None = None
        for item in candidates:
            start_row, start_col, end_row, end_col = item
            if not (0 <= start_row < len(self.lines) and 0 <= end_row < len(self.lines)):
                continue
            opener = self.lines[start_row][start_col] if 0 <= start_col < len(self.lines[start_row]) else ""
            if expected_open and opener != expected_open:
                continue
            if best is None:
                best = item
                continue
            best_span = (best[2] - best[0], best[3] - best[1])
            span = (end_row - start_row, end_col - start_col)
            if span < best_span:
                best = item
        if best is None:
            return None
        start_row, start_col, end_row, end_col = best
        if scope == "i":
            inner_start_col = start_col + 1
            inner_end_col = max(inner_start_col, end_col)
            return start_row, inner_start_col, end_row, inner_end_col
        return start_row, start_col, end_row, end_col + 1

    def _goto_definition(self) -> None:
        symbol = word_at_cursor(self._line(), self.cx)
        if not symbol:
            self._set_message("No symbol under cursor.", error=True)
            return

        if self._goto_definition_lsp(symbol):
            return

        pattern = re.compile(rf"^\s*(def|class)\s+{re.escape(symbol)}\b")
        for index, line in enumerate(self.lines):
            if pattern.search(line):
                self._record_jump_origin()
                self.cy = index
                self.cx = max(0, line.find(symbol))
                self._set_message(f"Definition: {symbol} (current file)")
                return

        self.file_index.refresh()
        for relative in self.file_index.list_files()[:2000]:
            path = self._workspace_root / relative
            if self.file_path is not None and path.resolve() == self.file_path.resolve():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for index, line in enumerate(lines):
                if pattern.search(line):
                    self._record_jump_origin()
                    if self.open_file(path, force=False):
                        self.cy = clamp(index, 0, len(self.lines) - 1)
                        self.cx = max(0, self._line().find(symbol))
                        self._set_message(f"Definition: {symbol} ({relative})")
                    return
        self._set_message(f"Definition not found: {symbol}", error=True)

    def _indent_lines(self, start: int, end: int) -> None:
        prefix = " " * self.tab_size
        for row in range(start, end + 1):
            self.lines[row] = prefix + self.lines[row]
            self.buffer.mark_dirty(row)
        if start <= self.cy <= end:
            self.cx += self.tab_size
        self._mark_modified()

    def _outdent_lines(self, start: int, end: int) -> None:
        for row in range(start, end + 1):
            line = self.lines[row]
            if line.startswith("\t"):
                self.lines[row] = line[1:]
            elif line.startswith(" "):
                strip_count = 0
                while strip_count < self.tab_size and strip_count < len(line) and line[strip_count] == " ":
                    strip_count += 1
                self.lines[row] = line[strip_count:]
            self.buffer.mark_dirty(row)
        if start <= self.cy <= end:
            self.cx = max(0, self.cx - self.tab_size)
        self._mark_modified()

    def _comment_rows(self) -> list[int]:
        selected = self._selected_line_range()
        if selected is not None:
            start, end = selected
            return list(range(start, end + 1))
        return self._target_edit_rows()

    def _toggle_comment(self) -> None:
        rows = self._comment_rows()
        if not rows:
            return
        prefix = self._syntax_manager().line_comment_for_file(self.file_path).strip()
        if not prefix:
            self._set_message("Current language has no line-comment prefix.", error=True)
            return

        non_empty = [row for row in rows if self.lines[row].strip()]
        if not non_empty:
            self._set_message("No non-empty lines to comment.")
            return

        def _is_commented(text: str) -> bool:
            stripped = text.lstrip()
            return stripped.startswith(prefix)

        should_uncomment = all(_is_commented(self.lines[row]) for row in non_empty)
        for row in rows:
            line = self.lines[row]
            if not line.strip():
                continue
            indent_len = len(line) - len(line.lstrip(" "))
            indent = line[:indent_len]
            body = line[indent_len:]
            if should_uncomment and body.startswith(prefix):
                body = body[len(prefix) :]
                if body.startswith(" "):
                    body = body[1:]
                self.lines[row] = indent + body
                self.buffer.mark_dirty(row)
            elif not should_uncomment:
                spacer = " " if body else ""
                self.lines[row] = f"{indent}{prefix}{spacer}{body}"
                self.buffer.mark_dirty(row)

        self._mark_modified()
        self._set_message("Comment toggled.")

    def _add_cursor_down(self) -> None:
        if self.cy >= len(self.lines) - 1 and not self.extra_cursor_lines:
            self._set_message("No lower line for extra cursor.")
            return

        anchor = max([self.cy, *self.extra_cursor_lines], default=self.cy)
        candidate = anchor + 1
        if candidate >= len(self.lines):
            self._set_message("No more lines for extra cursor.")
            return
        if candidate in self.extra_cursor_lines:
            self._set_message("Cursor already exists on target line.")
            return

        self.extra_cursor_lines.append(candidate)
        self.extra_cursor_lines.sort()
        self._set_message(f"Multi-cursor lines: {1 + len(self.extra_cursor_lines)}")

    def _add_cursor_next_match(self) -> bool:
        symbol = word_at_cursor(self._line(), self.cx)
        if not symbol:
            return False
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        selected_rows = {self.cy, *self.extra_cursor_lines}
        total = len(self.lines)
        start_row = self.cy
        start_col = self.cx + 1
        target: tuple[int, int] | None = None
        for current in range(start_row, total):
            line = self.lines[current]
            search_from = start_col if current == start_row else 0
            for match in pattern.finditer(line, search_from):
                if current in selected_rows:
                    continue
                target = (current, match.start())
                break
            if target is not None:
                break
        if target is None:
            for current in range(0, start_row):
                line = self.lines[current]
                for match in pattern.finditer(line):
                    if current in selected_rows:
                        continue
                    target = (current, match.start())
                    break
                if target is not None:
                    break
        if target is None:
            self._set_message(f"No more matches for '{symbol}'.")
            return True
        origin = self.cy
        if origin not in self.extra_cursor_lines:
            self.extra_cursor_lines.append(origin)
        self.cy, self.cx = target
        self.extra_cursor_lines = sorted({row for row in self.extra_cursor_lines if row != self.cy})
        self._set_message(f"Multi-cursor word '{symbol}': {1 + len(self.extra_cursor_lines)}")
        return True

    def _open_fuzzy(self, query: str = "") -> None:
        if not self.config.feature_enabled("fuzzy_finder"):
            self._set_message("Fuzzy finder is disabled in config.", error=True)
            return
        self.file_index.refresh()
        self.mode = MODE_FUZZY
        self.fuzzy_query = query
        self.fuzzy_index = 0
        self._floating_list = FloatingList(
            title="Fuzzy Finder  (Enter=open, Esc=cancel)",
            footer="Type to filter, Up/Down select",
        )
        self._floating_source = "fuzzy"
        self._update_fuzzy_matches()

    def _update_fuzzy_matches(self) -> None:
        all_files = self.file_index.list_files()
        self.fuzzy_matches = fuzzy_filter(all_files, self.fuzzy_query, limit=20)
        self.fuzzy_index = clamp(self.fuzzy_index, 0, max(0, len(self.fuzzy_matches) - 1))
        if self._floating_list is not None:
            self._floating_list.selected = self.fuzzy_index
            self._floating_list.set_items([str(path) for path in self.fuzzy_matches])
            self.fuzzy_index = self._floating_list.selected

    def _accept_fuzzy_selection(self) -> None:
        if not self.fuzzy_matches:
            self._set_message("No fuzzy match to open.", error=True)
            return
        if self._floating_list is not None:
            self.fuzzy_index = self._floating_list.selected
        target = self._workspace_root / self.fuzzy_matches[self.fuzzy_index]
        self._record_jump_origin()
        if self.open_file(target, force=False):
            self.mode = MODE_NORMAL
            self.fuzzy_query = ""
            self.fuzzy_matches = []
            self.fuzzy_index = 0
            self._floating_list = None
            self._floating_source = ""

    def _sync_file_tree_popup(self) -> None:
        entries: list[str] = []
        for entry in self._file_tree_feature.entries:
            if entry.is_dir:
                entries.append(f"  {entry.display}")
                continue
            marker = " "
            if self.config.feature_enabled("git_status"):
                marker = self.git.status_for_relative(Path(entry.relative_path)) or " "
                marker = marker if marker.strip() else " "
            entries.append(f"{marker} {entry.display}")
        if not entries:
            entries = ["(loading...)" if self._file_tree_task_active else "(empty)"]
        popup = self._floating_list
        if popup is None or self._floating_source != "file_tree":
            popup = FloatingList(
                title="EXPLORER",
                footer="<Esc> close  <Enter> open  <Up/Down> move  Tab/- fold  :tree status",
            )
            self._floating_list = popup
            self._floating_source = "file_tree"
        popup.set_items(entries)
        popup.selected = self._file_tree_feature.selected
        popup.scroll = self._file_tree_feature.scroll

    def _open_explorer(self, *, refresh: bool = False) -> None:
        if not self._file_tree_feature.enabled:
            self._set_message("File tree feature is disabled in config.", error=True)
            return
        self._file_tree_feature.open()
        self.mode = MODE_EXPLORER
        self._floating_accept_mode = MODE_NORMAL
        if refresh or not self._file_tree_feature.entries:
            self._schedule_file_tree_refresh()
        self._sync_file_tree_popup()

    def _close_explorer(self) -> None:
        self._file_tree_feature.close()
        self._floating_list = None
        self._floating_source = ""
        if self.mode == MODE_EXPLORER:
            self.mode = MODE_NORMAL

    def _completion_prefix_range(self) -> tuple[int, int, str]:
        line = self._line()
        start = self.cx
        while start > 0 and is_word_char(line[start - 1]):
            start -= 1
        end = self.cx
        while end < len(line) and is_word_char(line[end]):
            end += 1
        return start, end, line[start:self.cx]

    def _sync_completion_popup(self) -> None:
        popup = self._floating_list
        if popup is None or self._floating_source != "completion":
            popup = FloatingList(
                title="COMPLETION",
                footer="<Tab/Enter> accept  <Esc> close",
            )
            self._floating_list = popup
            self._floating_source = "completion"
        items = [
            self._format_completion_item(candidate, indices)
            for candidate, indices in self._completion_feature.items
        ]
        popup.set_items(items)
        popup.selected = self._completion_feature.selected
        popup.scroll = self._completion_feature.scroll

    def _format_completion_item(self, candidate: str, indices: list[int]) -> str:
        if not indices:
            return candidate
        highlights = set(indices)
        out: list[str] = []
        for index, char in enumerate(candidate):
            if index in highlights:
                out.append(char.upper())
            else:
                out.append(char)
        return "".join(out)

    def _snippet_completion_candidates(self, prefix: str) -> list[str]:
        language = self._syntax_profile.name.lower()
        shared = {
            "if",
            "for",
            "while",
            "try",
            "with",
            "class",
            "def",
            "return",
            "import",
            "from",
        }
        if language == "python":
            shared.update({"lambda", "yield", "async", "await", "except"})
        elif language in {"javascript", "typescript"}:
            shared.update({"function", "const", "let", "export", "interface"})
        clean = prefix.strip().lower()
        if not clean:
            return sorted(shared)
        return sorted(item for item in shared if item.lower().startswith(clean))

    def _path_completion_candidates(self, prefix: str) -> list[str]:
        clean = prefix.strip()
        if len(clean) < 1:
            return []
        root = self.file_path.parent if self.file_path is not None else self._workspace_root
        try:
            entries = list(root.iterdir())
        except OSError:
            return []
        out: list[str] = []
        for entry in entries:
            name = entry.name
            if not name.startswith(clean):
                continue
            out.append(f"{name}/" if entry.is_dir() else name)
            if len(out) >= 40:
                break
        return out

    def _completion_extra_candidates(self, prefix: str) -> list[str]:
        bucket: set[str] = set()
        bucket.update(self._snippet_completion_candidates(prefix))
        bucket.update(self._path_completion_candidates(prefix))
        bucket.update(self._lsp_completion_candidates())
        return sorted(item for item in bucket if item and len(item) >= 2)

    def _open_tab_completion(self) -> bool:
        if not self._completion_feature.enabled:
            return False
        start_col, end_col, prefix = self._completion_prefix_range()
        if not prefix:
            return False
        ast_hint = ""
        service = self._get_ast_query_service()
        if service is not None and self.file_path is not None:
            try:
                node = service.node_at(self.file_path, self.lines, self.cy + 1, self.cx + 1)
            except Exception:
                node = None
            if node is not None:
                ast_hint = f"{node.kind} {node.name or ''}"
        self._completion_feature.open(
            prefix,
            self.lines,
            ast_hint,
            extra_candidates=self._completion_extra_candidates(prefix),
        )
        if not self._completion_feature.visible:
            return False
        self._completion_prefix = prefix
        self._completion_insert_col = start_col
        self._completion_insert_row = self.cy
        self._completion_replace_end = end_col
        self._floating_accept_mode = MODE_INSERT
        self.mode = MODE_COMPLETION
        self._sync_completion_popup()
        return True

    def _close_tab_completion(self) -> None:
        self._completion_feature.close()
        self._completion_replace_end = 0
        self._floating_list = None
        self._floating_source = ""
        if self.mode == MODE_COMPLETION:
            self.mode = MODE_INSERT

    def _accept_tab_completion(self) -> None:
        value = self._completion_feature.selected_text()
        if not value:
            self._close_tab_completion()
            return
        row = self._completion_insert_row
        if not (0 <= row < len(self.lines)):
            self._close_tab_completion()
            return
        line = self.lines[row]
        start = self._completion_insert_col
        end = self._completion_replace_end if row == self.cy else start + len(self._completion_prefix)
        end = clamp(end, start, len(line))
        self.lines[row] = line[:start] + value + line[end:]
        self.cy = row
        self.cx = start + len(value)
        self.buffer.mark_dirty(row)
        self._mark_modified()
        self._close_tab_completion()

    def _open_plugin_list_popup(self) -> None:
        self._ensure_plugins_loaded()
        entries = self.plugins.list_plugins()
        if not entries:
            self._set_message("No plugins found.")
            return
        labels = [
            (
                f"{item['name']} v{item.get('version', '-')}"
                f" | loaded={item['loaded']}"
                f" | {item.get('description', '') or 'no description'}"
                f" | {item['error'] or 'ok'}"
            )
            for item in entries
        ]
        self._floating_list = FloatingList(
            title="Plugins (Enter=details, Esc=close)",
            footer="Use Up/Down to browse plugin records",
            items=labels,
        )
        self._floating_source = "plugin_list"
        self._floating_accept_mode = MODE_NORMAL
        self.mode = MODE_FLOAT_LIST

    def _toggle_sidebar(self) -> None:
        if not self.config.sidebar_enabled():
            self._set_message("Sidebar feature is disabled in config.", error=True)
            return
        self._sidebar_manual_override = True
        self.show_sidebar = not self.show_sidebar
        state = "on" if self.show_sidebar else "off"
        self._set_message(f"Sidebar: {state}")

    def _terminal_default_command(self) -> str:
        if os.name == "nt":
            return "cmd"
        shell = os.environ.get("SHELL", "").strip()
        return shell or "/bin/sh"

    def _active_terminal_session(self) -> TerminalSession | None:
        if self._terminal_process_id is None:
            return None
        return self._terminal_sessions.get(self._terminal_process_id)

    def _sync_active_terminal_refs(self) -> None:
        session = self._active_terminal_session()
        if session is None:
            self._terminal_output = []
            self._terminal_scroll = 0
            return
        self._terminal_output = session.output
        self._terminal_scroll = max(0, session.scroll)

    def _save_terminal_scroll(self) -> None:
        session = self._active_terminal_session()
        if session is None:
            return
        session.scroll = max(0, self._terminal_scroll)

    def _switch_terminal_session(self, process_id: int) -> bool:
        if process_id not in self._terminal_sessions:
            return False
        self._save_terminal_scroll()
        self._terminal_process_id = process_id
        self._sync_active_terminal_refs()
        return True

    def _visible_terminal_sessions(self) -> list[TerminalSession]:
        active = self._active_terminal_session()
        if active is None:
            return []
        if not self._terminal_split_view:
            return [active]
        other: TerminalSession | None = None
        for process_id in reversed(self._terminal_session_order):
            if process_id == active.process_id:
                continue
            candidate = self._terminal_sessions.get(process_id)
            if candidate is None:
                continue
            other = candidate
            break
        if other is None:
            return [active]
        return [active, other]

    def _terminal_scroll_to_output_index(self, session: TerminalSession, output_index: int) -> None:
        output_len = len(session.output)
        if output_len <= 0:
            session.scroll = 0
            if session.process_id == self._terminal_process_id:
                self._terminal_scroll = 0
            return
        safe_index = clamp(output_index, 0, output_len - 1)
        visible_rows = max(1, self._terminal_size()[1] - 8)
        target_end = min(output_len, safe_index + 1 + (visible_rows // 2))
        session.scroll = max(0, output_len - target_end)
        if session.process_id == self._terminal_process_id:
            self._terminal_scroll = session.scroll

    def _terminal_search(self, query: str) -> bool:
        session = self._active_terminal_session()
        if session is None:
            self._set_message("Terminal is not running.", error=True)
            return False
        clean = query.strip()
        if not clean:
            self._set_message("Usage: :term search <query>|next|prev|clear", error=True)
            return False
        lowered = clean.lower()
        hits = [index for index, line in enumerate(session.output) if lowered in line.lower()]
        session.search_query = clean
        session.search_hits = hits
        if not hits:
            session.search_index = -1
            self._set_message(f"Terminal search not found: {clean}", error=True)
            return False
        session.search_index = len(hits) - 1
        self._terminal_scroll_to_output_index(session, hits[session.search_index])
        self._set_message(f"Terminal search: {len(hits)} match(es) for '{clean}'")
        return True

    def _terminal_search_shift(self, delta: int) -> bool:
        session = self._active_terminal_session()
        if session is None:
            self._set_message("Terminal is not running.", error=True)
            return False
        if not session.search_hits:
            self._set_message("Terminal search: no active matches.", error=True)
            return False
        if session.search_index < 0:
            session.search_index = 0
        else:
            session.search_index = (session.search_index + delta) % len(session.search_hits)
        index = session.search_hits[session.search_index]
        self._terminal_scroll_to_output_index(session, index)
        self._set_message(
            f"Terminal search hit {session.search_index + 1}/{len(session.search_hits)}: '{session.search_query}'"
        )
        return True

    def _clear_terminal_search(self) -> bool:
        session = self._active_terminal_session()
        if session is None:
            self._set_message("Terminal is not running.", error=True)
            return False
        session.search_query = ""
        session.search_hits = []
        session.search_index = -1
        self._set_message("Terminal search cleared.")
        return True

    def _switch_terminal_relative(self, step: int) -> bool:
        order = [process_id for process_id in self._terminal_session_order if process_id in self._terminal_sessions]
        if not order:
            self._set_message("No terminal sessions.", error=True)
            return False
        current = self._terminal_process_id if self._terminal_process_id in order else order[0]
        index = order.index(current)
        target = order[(index + step) % len(order)]
        if not self._switch_terminal_session(target):
            self._set_message(f"Terminal session missing: {target}", error=True)
            return False
        self.mode = MODE_TERMINAL
        self._set_message(f"Terminal switched: {target}")
        return True

    def _terminal_session_rows(self, session: TerminalSession, rows: int) -> list[str]:
        if rows <= 0:
            return []
        status = self._process_manager.status(session.process_id)
        output_rows = max(0, rows - 1)
        total = len(session.output)
        end = max(0, total - max(0, session.scroll))
        start = max(0, end - output_rows)
        highlighted = set(session.search_hits)
        body: list[str] = []
        for index in range(start, end):
            text = session.output[index]
            if index in highlighted:
                text = f"? {text}"
            body.append(text)
        while len(body) < output_rows:
            body.append("")
        header = f"[{session.process_id} {status}] {session.command}"
        return [header, *body[:output_rows]]

    def _open_terminal(self, command: str | None = None, *, force_new: bool = False) -> bool:
        active = self._active_terminal_session()
        if (
            not force_new
            and active is not None
            and self._process_manager.status(active.process_id) == "running"
        ):
            self.mode = MODE_TERMINAL
            self._set_message(f"Terminal resumed: {active.process_id}")
            return True
        command_text = command.strip() if isinstance(command, str) and command.strip() else self._terminal_default_command()
        try:
            process_id = self._process_manager.start(command_text, cwd=str(self._workspace_root))
        except Exception as exc:
            self._set_message(f"Terminal start failed: {exc}", error=True)
            return False
        session = TerminalSession(
            process_id=process_id,
            command=command_text,
            output=[f"[term:{process_id}] {command_text}"],
        )
        self._terminal_sessions[process_id] = session
        if process_id not in self._terminal_session_order:
            self._terminal_session_order.append(process_id)
        self._switch_terminal_session(process_id)
        self._terminal_input = ""
        self._terminal_split_view = self._terminal_split_view and len(self._terminal_sessions) >= 2
        self.mode = MODE_TERMINAL
        self._set_message(f"Terminal started: {process_id}")
        return True

    def _send_terminal_input(self, text: str) -> None:
        session = self._active_terminal_session()
        if session is None:
            self._set_message("Terminal is not running.", error=True)
            return
        if self._process_manager.status(session.process_id) != "running":
            self._set_message(f"Terminal is not running: {session.process_id}", error=True)
            return
        if text in {"\u0003", "\u0004"}:
            payload = text
        else:
            payload = text.rstrip("\n")
            if payload:
                session.output.append(f"> {payload}")
                if len(session.output) > 2000:
                    session.output = session.output[-2000:]
                if session.process_id == self._terminal_process_id:
                    self._terminal_output = session.output
        if not self._process_manager.write(session.process_id, payload):
            self._set_message("Terminal input failed.", error=True)

    def _close_terminal(self, *, kill: bool) -> None:
        session = self._active_terminal_session()
        if session is not None and kill:
            self._process_manager.stop_sync(session.process_id, timeout=0.6)
            if session.process_id == self._dap_session_process_id:
                self._dap_session_process_id = None
                self._dap_target_path = None
            session.output.append("[term] stop requested")
            if len(session.output) > 2000:
                session.output = session.output[-2000:]
            if session.process_id == self._terminal_process_id:
                self._terminal_output = session.output
        self._save_terminal_scroll()
        self._terminal_input = ""
        self.mode = MODE_NORMAL

    def _search_query_from_command_text(self) -> str:
        if self._command_prompt != "/":
            return ""
        if not self.command_text.startswith("find "):
            return ""
        return self.command_text[5:]

    def _push_search_history(self, query: str) -> None:
        clean = query.strip()
        if not clean:
            return
        try:
            self._search_history.remove(clean)
        except ValueError:
            pass
        self._search_history.append(clean)
        self._last_search_query = clean
        self._search_history_index = len(self._search_history)
        self._search_history_draft = ""

    def _count_search_matches(self, query: str) -> int:
        if not query:
            return 0
        total = 0
        for line in self.lines:
            total += line.count(query)
        return total

    def _on_command_text_changed(self) -> None:
        if self._command_prompt != "/":
            return
        query = self._search_query_from_command_text()
        self._incremental_search_query = query
        if self._search_preview_origin is None:
            self._search_preview_origin = self._capture_view_state()
        if not query:
            if self._search_preview_origin is not None:
                self._apply_view_state(self._search_preview_origin)
            self._set_message("Search: type query")
            return
        if not self.lines:
            self._set_message("Search failed: empty buffer.", error=True)
            return
        origin = self._search_preview_origin
        start_row = self.cy
        start_col = self.cx
        if origin is not None:
            start_col, start_row, _row_offset, _col_offset = origin
        start_row = clamp(start_row, 0, len(self.lines) - 1)
        start_col = max(0, start_col)
        location = find_next(self.lines, query, start_row, start_col)
        if location is None:
            self._set_message(f"Not found: {query}", error=True)
            return
        self.cy, self.cx = location
        self._set_message(f"Search preview: {self._count_search_matches(query)} match(es)")

    def _cancel_search_prompt(self) -> None:
        if self._search_preview_origin is not None:
            self._apply_view_state(self._search_preview_origin)
        self._search_preview_origin = None
        self._incremental_search_query = ""
        self._search_history_index = len(self._search_history)
        self._search_history_draft = ""

    def _submit_search_prompt(self, command_text: str | None = None) -> None:
        raw = command_text if isinstance(command_text, str) else self.command_text
        query = raw[5:].strip() if raw.startswith("find ") else raw.strip()
        self._search_preview_origin = None
        self._incremental_search_query = ""
        self._search_history_index = len(self._search_history)
        self._search_history_draft = ""
        if not query:
            self._set_message("Usage: /<text>", error=True)
            return
        self._find(query, include_current=True)

    def _browse_search_history(self, step: int) -> None:
        if self._command_prompt != "/":
            return
        if not self._search_history:
            return
        if step == 0:
            return
        history = list(self._search_history)
        max_index = len(history)
        if self._search_history_index == max_index:
            self._search_history_draft = self._search_query_from_command_text()
        if step < 0:
            self._search_history_index = max(0, self._search_history_index - 1)
        else:
            self._search_history_index = min(max_index, self._search_history_index + 1)
        if self._search_history_index >= max_index:
            query = self._search_history_draft
        else:
            query = history[self._search_history_index]
        self.command_text = f"find {query}"
        self._on_command_text_changed()

    def _find(self, query: str, *, include_current: bool = False) -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not query:
            self._set_message("Usage: :find <text>", error=True)
            return False

        start_col = self.cx if include_current else self.cx + 1
        location = find_next(self.lines, query, self.cy, start_col)
        if location is None:
            self._set_message(f"Not found: {query}", error=True)
            return False

        self.cy, self.cx = location
        self._push_search_history(query)
        self._set_message(f"Found: {query}")
        return True

    def _replace_next(self, old: str, new: str) -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not old:
            self._set_message("Usage: :replace <old> <new>", error=True)
            return False

        updated, location, changed = replace_next(self.lines, old, new, self.cy, self.cx)
        if not changed or location is None:
            self._set_message(f"Not found: {old}", error=True)
            return False

        self.lines = updated
        self.cy, self.cx = location
        self._mark_modified()
        self._set_message("Replaced next match.")
        return True

    def _replace_all(self, old: str, new: str) -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not old:
            self._set_message("Usage: :replaceall <old> <new>", error=True)
            return False

        updated, count = replace_all(self.lines, old, new)
        if count == 0:
            self._set_message(f"Not found: {old}", error=True)
            return False

        self.lines = updated
        self._mark_modified()
        self._set_message(f"Replaced {count} occurrence(s).")
        return True

    def _replace_all_project(self, old: str, new: str) -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not old:
            self._set_message("Usage: :replaceproj <old> <new>", error=True)
            return False
        self.file_index.refresh(force=True)
        total = 0
        changed_files = 0
        skipped = 0
        current_path = self.file_path.resolve() if self.file_path is not None else None
        for relative in self.file_index.list_files():
            path = (self._workspace_root / relative).resolve()
            if current_path is not None and path == current_path and self.modified:
                skipped += 1
                continue
            try:
                raw, encoding = self._decode_file_bytes(path.read_bytes())
            except (OSError, UnicodeDecodeError):
                continue
            normalized, line_ending = self._normalize_loaded_text(raw)
            updated, count = replace_all(normalized.split("\n"), old, new)
            if count <= 0:
                continue
            serialized = "\n".join(updated).replace("\n", line_ending)
            try:
                path.write_text(serialized, encoding=encoding, newline="")
            except OSError:
                skipped += 1
                continue
            total += count
            changed_files += 1
            if current_path is not None and path == current_path and not self.modified:
                self.lines = updated if updated else [""]
                self.buffer.mark_all_dirty()
                self.cy = clamp(self.cy, 0, len(self.lines) - 1)
                self.cx = clamp(self.cx, 0, len(self._line()))
        if total <= 0:
            self._set_message(f"Project replace not found: {old}", error=True)
            return False
        detail = f"files={changed_files} replacements={total}"
        if skipped > 0:
            detail += f" skipped={skipped}"
        self._set_message(f"Project replace done: {detail}")
        return True

    def _regex_flags(self, text: str) -> int | None:
        flags = 0
        for ch in text.strip().lower():
            if ch == "i":
                flags |= re.IGNORECASE
            elif ch == "m":
                flags |= re.MULTILINE
            elif ch == "s":
                flags |= re.DOTALL
            elif ch == "":
                continue
            else:
                return None
        return flags

    def _compile_regex(self, pattern: str, flags_text: str) -> re.Pattern[str] | None:
        flags = self._regex_flags(flags_text)
        if flags is None:
            self._set_message(f"Invalid regex flags: {flags_text}", error=True)
            return None
        try:
            return re.compile(pattern, flags)
        except re.error as exc:
            self._set_message(f"Regex compile failed: {exc}", error=True)
            return None

    def _find_regex(self, pattern: str, flags_text: str = "") -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not pattern:
            self._set_message("Usage: :findre <pattern> [flags]", error=True)
            return False
        compiled = self._compile_regex(pattern, flags_text)
        if compiled is None:
            return False
        row = self.cy
        for current in range(row, len(self.lines)):
            start = self.cx + 1 if current == row else 0
            match = compiled.search(self.lines[current], start)
            if match is not None:
                self.cy = current
                self.cx = match.start()
                self._set_message(f"Regex found: /{pattern}/")
                return True
        for current in range(0, row):
            match = compiled.search(self.lines[current], 0)
            if match is not None:
                self.cy = current
                self.cx = match.start()
                self._set_message(f"Regex found: /{pattern}/")
                return True
        self._set_message(f"Regex not found: /{pattern}/", error=True)
        return False

    def _replace_regex_next(self, pattern: str, replacement: str, flags_text: str = "") -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not pattern:
            self._set_message("Usage: :replacere <pattern> <replacement> [flags]", error=True)
            return False
        compiled = self._compile_regex(pattern, flags_text)
        if compiled is None:
            return False
        row = self.cy
        for current in range(row, len(self.lines)):
            start = self.cx if current == row else 0
            match = compiled.search(self.lines[current], start)
            if match is None:
                continue
            replaced = match.expand(replacement)
            line = self.lines[current]
            self.lines[current] = line[: match.start()] + replaced + line[match.end() :]
            self.cy = current
            self.cx = match.start() + len(replaced)
            self._mark_modified()
            self._set_message("Regex replace next done.")
            return True
        for current in range(0, row):
            match = compiled.search(self.lines[current], 0)
            if match is None:
                continue
            replaced = match.expand(replacement)
            line = self.lines[current]
            self.lines[current] = line[: match.start()] + replaced + line[match.end() :]
            self.cy = current
            self.cx = match.start() + len(replaced)
            self._mark_modified()
            self._set_message("Regex replace next done.")
            return True
        self._set_message(f"Regex not found: /{pattern}/", error=True)
        return False

    def _replace_regex_all(self, pattern: str, replacement: str, flags_text: str = "") -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not pattern:
            self._set_message("Usage: :replaceallre <pattern> <replacement> [flags]", error=True)
            return False
        compiled = self._compile_regex(pattern, flags_text)
        if compiled is None:
            return False
        total = 0
        updated: list[str] = []
        for line in self.lines:
            replaced, count = compiled.subn(replacement, line)
            updated.append(replaced)
            total += count
        if total == 0:
            self._set_message(f"Regex not found: /{pattern}/", error=True)
            return False
        self.lines = updated
        self._mark_modified()
        self._set_message(f"Regex replaced {total} occurrence(s).")
        return True

    def _replace_regex_all_project(self, pattern: str, replacement: str, flags_text: str = "") -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not pattern:
            self._set_message("Usage: :replaceprojre <pattern> <replacement> [flags]", error=True)
            return False
        compiled = self._compile_regex(pattern, flags_text)
        if compiled is None:
            return False
        self.file_index.refresh(force=True)
        total = 0
        changed_files = 0
        skipped = 0
        current_path = self.file_path.resolve() if self.file_path is not None else None
        for relative in self.file_index.list_files():
            path = (self._workspace_root / relative).resolve()
            if current_path is not None and path == current_path and self.modified:
                skipped += 1
                continue
            try:
                raw, encoding = self._decode_file_bytes(path.read_bytes())
            except (OSError, UnicodeDecodeError):
                continue
            normalized, line_ending = self._normalize_loaded_text(raw)
            changed = 0
            updated_lines: list[str] = []
            for line in normalized.split("\n"):
                replaced, count = compiled.subn(replacement, line)
                updated_lines.append(replaced)
                changed += count
            if changed <= 0:
                continue
            serialized = "\n".join(updated_lines).replace("\n", line_ending)
            try:
                path.write_text(serialized, encoding=encoding, newline="")
            except OSError:
                skipped += 1
                continue
            total += changed
            changed_files += 1
            if current_path is not None and path == current_path and not self.modified:
                self.lines = updated_lines if updated_lines else [""]
                self.buffer.mark_all_dirty()
                self.cy = clamp(self.cy, 0, len(self.lines) - 1)
                self.cx = clamp(self.cx, 0, len(self._line()))
        if total <= 0:
            self._set_message(f"Project regex not found: /{pattern}/", error=True)
            return False
        detail = f"files={changed_files} replacements={total}"
        if skipped > 0:
            detail += f" skipped={skipped}"
        self._set_message(f"Project regex replace done: {detail}")
        return True

    def _rename_symbol(self, old: str, new: str) -> bool:
        if not self.config.feature_enabled("refactor_tools"):
            self._set_message("Refactor tools are disabled in config.", error=True)
            return False
        if not old or not new:
            self._set_message("Usage: :rename <old> <new>", error=True)
            return False

        updated, count = rename_symbol(self.lines, old, new)
        if count == 0:
            self._set_message(f"Symbol not found: {old}", error=True)
            return False

        self.lines = updated
        self._mark_modified()
        self._set_message(f"Renamed {count} occurrence(s).")
        return True

    def _format_code(self) -> bool:
        if not self.config.feature_enabled("code_style_normalizer"):
            self._set_message("Code style normalizer is disabled in config.", error=True)
            return False
        language = self._syntax_profile.name.lower()
        formatted, changed = normalize_code_style(
            self.lines,
            tab_size=self.tab_size,
            language=language,
            organize_imports_enabled=self.config.feature_enabled("refactor_tools"),
        )
        self.lines = formatted
        if changed > 0:
            self._mark_modified()
        self._set_message(f"Style normalized. Changed lines: {changed}")
        return True

    def _refactor_imports(self) -> bool:
        if not self.config.feature_enabled("refactor_tools"):
            self._set_message("Refactor tools are disabled in config.", error=True)
            return False
        if self._syntax_profile.name.lower() != "python":
            self._set_message("Import organization is currently Python-only.", error=True)
            return False

        updated, changed = organize_python_imports(self.lines)
        self.lines = updated
        if changed > 0:
            self._mark_modified()
        self._set_message(f"Import organization finished. Changed lines: {changed}")
        return True

    def _enter_command(self, initial: str = "", *, prompt: str = ":") -> None:
        self.mode = MODE_COMMAND
        self.command_text = initial
        self._command_prompt = prompt
        self.pending_operator = ""
        self.visual_anchor = None
        if prompt == "/":
            self._search_preview_origin = self._capture_view_state()
            self._search_history_index = len(self._search_history)
            self._search_history_draft = ""
            self._incremental_search_query = ""
            self._on_command_text_changed()
        else:
            self._search_preview_origin = None
            self._incremental_search_query = ""

    def _open_rename_prompt(self) -> None:
        symbol = word_at_cursor(self._line(), self.cx)
        if not symbol:
            self._set_message("No symbol under cursor.", error=True)
            return
        self._enter_command(f"rename {symbol} ")

    def _run_script_file(self, target: str) -> bool:
        if not self.config.feature_enabled("scripting"):
            self._set_message("Scripting is disabled in config.", error=True)
            return False
        self._ensure_plugins_loaded()
        path = self._resolve_path(target)
        try:
            result = self.plugins.execute_script(path)
        except ScriptError as exc:
            self._show_alert(f"Script error: {exc}")
            return False
        except Exception as exc:
            self._show_alert(f"Script host error: {exc}")
            return False
        self._set_message(result)
        return True

    def _profile_script_file(self, target: str) -> bool:
        if not self.config.feature_enabled("scripting"):
            self._set_message("Scripting is disabled in config.", error=True)
            return False
        self._ensure_plugins_loaded()
        path = self._resolve_path(target)
        profiler = cProfile.Profile()
        try:
            profiler.enable()
            result = self.plugins.execute_script(path)
            profiler.disable()
        except ScriptError as exc:
            self._show_alert(f"Script error: {exc}")
            return False
        except Exception as exc:
            self._show_alert(f"Script host error: {exc}")
            return False

        stats_stream = io.StringIO()
        stats = pstats.Stats(profiler, stream=stats_stream).sort_stats("cumtime")
        stats.print_stats(self.config.profile_top_n())
        profile_text = stats_stream.getvalue()
        summary = f"{result}\n\n{profile_text}"
        self._show_alert(summary[:7000])
        return True

    def _handle_plugin_command(self, args: list[str]) -> bool:
        if not self.config.feature_enabled("plugins"):
            self._set_message("Plugin system is disabled in config.", error=True)
            return False
        self._ensure_plugins_loaded()
        if not args:
            self._set_message("Usage: :plugin list|load|install|uninstall|run ...", error=True)
            return False

        action = args[0]
        if action == "list":
            self._open_plugin_list_popup()
            return True

        if action in {"load", "reload"}:
            if len(args) == 1:
                results = self.plugins.load_all()
                for message in results:
                    if "error" in message.lower():
                        self._show_alert(message)
                        return False
                self._set_message(results[-1] if results else "Plugins loaded.")
                return True
            message = self.plugins.load_plugin(args[1])
            if "error" in message.lower():
                self._show_alert(message)
                return False
            self._set_message(message)
            return True

        if action == "install":
            if len(args) < 2:
                self._set_message("Usage: :plugin install <path>", error=True)
                return False
            source = self._resolve_path(args[1])
            try:
                message = self.plugins.install(source)
            except ScriptError as exc:
                self._show_alert(f"Plugin install error: {exc}")
                return False
            except Exception as exc:
                self._show_alert(f"Plugin install error: {exc}")
                return False
            self._set_message(message)
            return True

        if action in {"uninstall", "remove", "rm"}:
            if len(args) < 2:
                self._set_message("Usage: :plugin uninstall <name>", error=True)
                return False
            try:
                message = self.plugins.uninstall(args[1])
            except ScriptError as exc:
                self._show_alert(f"Plugin uninstall error: {exc}")
                return False
            except Exception as exc:
                self._show_alert(f"Plugin uninstall error: {exc}")
                return False
            self._set_message(message)
            return True

        if action == "run":
            if len(args) < 3:
                self._set_message("Usage: :plugin run <plugin> <function> [args ...]", error=True)
                return False
            plugin_name = args[1]
            function_name = args[2]
            payload = [self._coerce_script_arg(item) for item in args[3:]]
            try:
                result = self.plugins.run(plugin_name, function_name, payload)
            except ScriptError as exc:
                self._show_alert(f"Plugin runtime error: {exc}")
                return False
            except Exception as exc:
                self._show_alert(f"Plugin runtime error: {exc}")
                return False
            self._set_message(f"Plugin result: {result}")
            return True

        self._set_message("Usage: :plugin list|load|install|uninstall|run ...", error=True)
        return False

    def _coerce_script_arg(self, value: str) -> object:
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered == "null":
            return None
        if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
            try:
                return int(value)
            except ValueError:
                return value
        try:
            if "." in value:
                return float(value)
        except ValueError:
            return value
        return value

    def open_project(self, target: Path | str, *, force: bool, startup: bool = False) -> bool:
        if self.modified and not force and not startup:
            self._set_message("Unsaved changes. Save file or use :q! before opening another project.", error=True)
            return False
        path = self._resolve_path(target)
        if not path.exists() or not path.is_dir():
            self._set_message(f"Project open failed: {path} is not a directory", error=True)
            return False
        self.file_path = None
        self._project_mode = True
        self._workspace_root = path.resolve()
        self.file_index = FileIndex(self._workspace_root, max_files=self.config.file_scan_limit())
        self.git = GitStatusProvider(
            self._workspace_root,
            enabled=self.config.feature_enabled("git_status"),
            refresh_seconds=self.config.git_refresh_seconds(),
        )
        self.lines = [""]
        self.buffer.configure_piece_table(False)
        self.cx = 0
        self.cy = 0
        self.row_offset = 0
        self.col_offset = 0
        self.modified = False
        self.pending_operator = ""
        self.pending_scope = ""
        self._pending_motion = ""
        self.visual_anchor = None
        self._clear_multi_cursor()
        self._jump_back_stack = []
        self._jump_forward_stack = []
        self._history.clear()
        self._history.set_root_snapshot(self._capture_snapshot())
        self._refresh_filetype_autocmds(None)
        self._syntax_profile = self._syntax_manager().profile_for_file(self.file_path)
        self._sync_incremental_syntax(force=True)
        self._reset_incremental_selection()
        current_view = self._capture_view_state()
        self._split_main_view = current_view
        self._split_secondary_view = current_view
        if not self._sidebar_manual_override:
            self.show_sidebar = self.config.sidebar_enabled()
        project_label = f"{path.name}/"
        self._tab_items = [project_label]
        self._current_tab_index = 0
        self._schedule_file_tree_refresh()
        self._schedule_git_control_refresh(force=True)
        self._set_message(f"Opened project: {path}")
        return True

    def open_file(self, target: Path | str, *, force: bool, startup: bool = False) -> bool:
        if self.modified and not force and not startup:
            self._set_message("Unsaved changes. Use :e! <file> to force.", error=True)
            return False

        path = self._resolve_path(target)
        self._refresh_filetype_autocmds(path)
        self._run_autocmds("bufreadpre")
        if path.exists() and path.is_dir():
            return self.open_project(path, force=force, startup=startup)
        text = ""
        if path.exists():
            try:
                raw, decoded_encoding = self._decode_file_bytes(path.read_bytes())
                normalized, detected_line_ending = self._normalize_loaded_text(raw)
                text = normalized
                self._current_encoding = decoded_encoding
                if self.config.preserve_line_ending():
                    self._current_line_ending = detected_line_ending
                else:
                    self._current_line_ending = self.config.default_line_ending()
            except (OSError, UnicodeDecodeError) as exc:
                self._set_message(f"Open failed: {exc}", error=True)
                return False
            self._set_message(f"Opened {path}")
        else:
            self._set_message(f"New file: {path}")
            self._current_line_ending = self.config.default_line_ending()
            self._current_encoding = self._encoding_candidates[0]

        self.file_path = path
        self._project_mode = False
        self._workspace_root = self._detect_workspace_root(path)
        self.file_index = FileIndex(self._workspace_root, max_files=self.config.file_scan_limit())
        self.git = GitStatusProvider(
            self._workspace_root,
            enabled=self.config.feature_enabled("git_status"),
            refresh_seconds=self.config.git_refresh_seconds(),
        )
        self.lines = text.split("\n")
        if not self.lines:
            self.lines = [""]
        use_piece_table = self.config.piece_table_enabled() and len(self.lines) >= self.config.piece_table_line_threshold()
        self.buffer.configure_piece_table(use_piece_table)
        if use_piece_table:
            self._set_message(f"Opened {path} (piece-table mode)")

        self.cx = 0
        self.cy = 0
        self.row_offset = 0
        self.col_offset = 0
        self.modified = False
        self.pending_operator = ""
        self.pending_scope = ""
        self._pending_motion = ""
        self.visual_anchor = None
        self._clear_multi_cursor()
        self._history.clear()
        self._history.set_root_snapshot(self._capture_snapshot())
        self._refresh_filetype_autocmds(self.file_path)
        self._syntax_profile = self._syntax_manager().profile_for_file(self.file_path)
        self._sync_incremental_syntax(force=True)
        self._reset_incremental_selection()
        current_view = self._capture_view_state()
        self._split_main_view = current_view
        self._split_secondary_view = current_view
        if self.file_path is not None:
            tab_label = self.file_path.name
            if tab_label not in self._tab_items:
                self._tab_items.append(tab_label)
            self._current_tab_index = self._tab_items.index(tab_label)
        self._schedule_git_control_refresh(force=True)
        self._maybe_prompt_swap_recovery(path)
        if not self._sidebar_manual_override:
            self.show_sidebar = False
        self._run_autocmds("bufreadpost")
        return True

    def save_file(self, target: Path | str | None = None, *, quiet: bool = False) -> bool:
        if target is not None:
            self.file_path = self._resolve_path(target)
            self._workspace_root = self._detect_workspace_root(self.file_path)
            self.file_index = FileIndex(self._workspace_root, max_files=self.config.file_scan_limit())
            self.git = GitStatusProvider(
                self._workspace_root,
                enabled=self.config.feature_enabled("git_status"),
                refresh_seconds=self.config.git_refresh_seconds(),
            )

        if self.file_path is None:
            self._set_message("No file name. Use :w <path>.", error=True)
            return False

        self._run_autocmds("bufwritepre")
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            current = self.buffer.text()
            target_newline = self._current_line_ending if self.config.preserve_line_ending() else self.config.default_line_ending()
            data = current.replace("\n", target_newline)
            self.file_path.write_text(data, encoding=self._current_encoding, newline="")
        except OSError as exc:
            self._set_message(f"Write failed: {exc}", error=True)
            return False

        self.modified = False
        self._last_auto_save = time.monotonic()
        if self.file_path is not None:
            tab_label = self.file_path.name
            if tab_label not in self._tab_items:
                self._tab_items.append(tab_label)
            self._current_tab_index = self._tab_items.index(tab_label)
        self._schedule_git_control_refresh(force=True)
        if self.file_path is not None:
            self._persistence.remove_swap(self.file_path)
        if not quiet:
            self._set_message(f"Wrote {self.file_path}")
        self._run_autocmds("bufwritepost")
        return True

    def request_quit(self, *, force: bool) -> bool:
        if self.modified and not force:
            self._write_swap_if_needed(force=True)
            self._set_message("No write since last change. Use :q! to discard.", error=True)
            return False
        self._save_session()
        if self.file_path is not None and not self.modified:
            self._persistence.remove_swap(self.file_path)
        self.running = False
        return True

    def _mode_style(self) -> str:
        if self.mode == MODE_INSERT:
            return self.theme.ui_style("mode_insert")
        if self.mode in {MODE_COMMAND, MODE_FUZZY, MODE_FLOAT_LIST, MODE_EXPLORER, MODE_COMPLETION, MODE_KEY_HINTS, MODE_ALERT}:
            return self.theme.ui_style("mode_command")
        if self.mode == MODE_TERMINAL:
            return self.theme.ui_style("mode_command")
        if self.mode == MODE_VISUAL:
            return self.theme.ui_style("mode_command")
        return self.theme.ui_style("mode_normal")

    def _box_chars(self) -> tuple[str, str, str, str, str, str]:
        if self._unicode_ui:
            return "╭", "╮", "╰", "╯", "│", "─"
        return "+", "+", "+", "+", "|", "-"

    def _render_popup_list_row(
        self,
        screen_row: int,
        text_rows: int,
        text_cols: int,
        fallback_title: str,
    ) -> str:
        base_style = self.theme.ui_style("editor")
        selected_style = self.theme.ui_style("fuzzy_selected")
        border_style = self.theme.ui_style("command_line")
        popup = self._floating_list
        title = popup.title if popup is not None else fallback_title
        footer = popup.footer if popup is not None else ""

        if popup is None:
            return f"{base_style}{' ' * text_cols}{RESET}"

        top_left, top_right, bottom_left, bottom_right, vertical, horizontal = self._box_chars()
        longest_item = max((display_width(item) for item in popup.items), default=20)
        content_width = max(display_width(title) + 2, display_width(footer) + 2, longest_item + 2)
        popup_width = clamp(content_width + 2, 24, max(24, text_cols - 2))
        popup_height = clamp(min(text_rows - 2, len(popup.items) + 4), 6, max(6, text_rows - 1))
        top = max(0, (text_rows - popup_height) // 2)
        left = max(0, (text_cols - popup_width) // 2)
        right_pad = max(0, text_cols - left - popup_width)
        blank = " " * text_cols

        if screen_row < top or screen_row >= top + popup_height:
            return f"{base_style}{blank}{RESET}"

        row = screen_row - top
        content_width = popup_width - 2
        if row == 0:
            heading = f" {title} "
            heading = slice_by_display(heading, 0, content_width)
            heading = pad_to_display(heading, content_width)
            top_line = top_left + heading + top_right
            return f"{border_style}{(' ' * left) + top_line + (' ' * right_pad)}{RESET}"

        if row == popup_height - 1:
            bottom_line = bottom_left + (horizontal * content_width) + bottom_right
            return f"{border_style}{(' ' * left) + bottom_line + (' ' * right_pad)}{RESET}"

        if row == popup_height - 2 and footer:
            footer_text = pad_to_display(slice_by_display(footer, 0, content_width), content_width)
            footer_line = vertical + footer_text + vertical
            return f"{border_style}{(' ' * left) + footer_line + (' ' * right_pad)}{RESET}"

        index = popup.scroll + row - 1
        if 0 <= index < len(popup.items):
            text = pad_to_display(slice_by_display(popup.items[index], 0, content_width), content_width)
            middle_line = vertical + text + vertical
            style = selected_style if index == popup.selected else base_style
            return f"{style}{(' ' * left) + middle_line + (' ' * right_pad)}{RESET}"
        middle_line = vertical + (" " * content_width) + vertical
        return f"{base_style}{(' ' * left) + middle_line + (' ' * right_pad)}{RESET}"

    def _render_fuzzy_row(self, screen_row: int, text_rows: int, text_cols: int) -> str:
        return self._render_popup_list_row(screen_row, text_rows, text_cols, "Fuzzy Finder")

    def _render_float_list_row(self, screen_row: int, text_rows: int, text_cols: int) -> str:
        return self._render_popup_list_row(screen_row, text_rows, text_cols, "Select Item")

    def _render_key_hint_row(self, screen_row: int, text_cols: int) -> str:
        base_style = self.theme.ui_style("editor")
        selected_style = self.theme.ui_style("fuzzy_selected")

        if screen_row == 0:
            head = "Keyboard Hints (Up/Down scroll, Esc close)"
            return f"{selected_style}{pad_to_display(slice_by_display(head, 0, text_cols), text_cols)}{RESET}"

        content_row = self.key_hint_scroll + screen_row - 1
        if 0 <= content_row < len(self.key_hint_lines):
            text = self.key_hint_lines[content_row]
            return f"{base_style}{pad_to_display(slice_by_display(text, 0, text_cols), text_cols)}{RESET}"

        return f"{base_style}{' ' * text_cols}{RESET}"

    def _render_alert_row(self, screen_row: int, text_cols: int) -> str:
        base_style = self.theme.ui_style("editor")
        error_style = self.theme.ui_style("message_error")

        if screen_row == 0:
            head = "Script Error (Esc/Enter close)"
            return f"{error_style}{pad_to_display(slice_by_display(head, 0, text_cols), text_cols)}{RESET}"

        content_row = screen_row - 1
        if 0 <= content_row < len(self.alert_lines):
            text = self.alert_lines[content_row]
            return f"{base_style}{pad_to_display(slice_by_display(text, 0, text_cols), text_cols)}{RESET}"
        return f"{base_style}{' ' * text_cols}{RESET}"

    def _line_segment_count(self, line_index: int, text_cols: int) -> int:
        if not (0 <= line_index < len(self.lines)):
            return 1
        width = max(1, display_width(self.lines[line_index]))
        return max(1, (width + text_cols - 1) // text_cols)

    def _visual_slot(self, screen_row: int, text_cols: int) -> tuple[int, int] | None:
        if not self._soft_wrap_enabled:
            return self.row_offset + screen_row, self.col_offset
        visual_row = 0
        line_index = self.row_offset
        while line_index < len(self.lines):
            segments = self._line_segment_count(line_index, text_cols)
            for segment in range(segments):
                if visual_row == screen_row:
                    return line_index, segment * text_cols
                visual_row += 1
            line_index += 1
        return None

    def _cursor_softwrap_row(self, text_cols: int) -> int:
        row = 1
        for line_index in range(self.row_offset, self.cy):
            row += self._line_segment_count(line_index, text_cols)
        cursor_display = display_width(self.lines[self.cy][: self.cx])
        row += cursor_display // max(1, text_cols)
        return row

    def _render_editor_row(
        self,
        screen_row: int,
        text_rows: int,
        gutter: int,
        text_cols: int,
    ) -> str:
        if self.mode == MODE_FUZZY:
            return self._render_fuzzy_row(screen_row, text_rows, text_cols + gutter)
        if self.mode in {MODE_FLOAT_LIST, MODE_EXPLORER, MODE_COMPLETION}:
            return self._render_float_list_row(screen_row, text_rows, text_cols + gutter)
        if self.mode == MODE_KEY_HINTS:
            return self._render_key_hint_row(screen_row, text_cols + gutter)
        if self.mode == MODE_ALERT:
            return self._render_alert_row(screen_row, text_cols + gutter)
        if self.mode == MODE_TERMINAL:
            return self._render_terminal_row(screen_row, text_rows, text_cols + gutter)
        if self._should_render_dashboard():
            return self._render_dashboard_row(screen_row, text_rows, gutter, text_cols)

        slot = self._visual_slot(screen_row, text_cols)
        if slot is not None:
            line_index, start_display = slot
            raw = self.lines[line_index]
            visible_code = slice_by_display(raw, start_display, text_cols)
            ghost_chunks = self.buffer.get_virtual_text(line_index)
            ghost_visible = ""
            if ghost_chunks:
                ghost_raw = "  <<" + " | ".join(ghost_chunks) + ">>"
                room = max(0, text_cols - display_width(visible_code))
                ghost_visible = slice_by_display(ghost_raw, 0, room)
            padding = " " * max(0, text_cols - display_width(visible_code) - display_width(ghost_visible))

            is_current = line_index == self.cy and self.mode not in {
                MODE_COMMAND,
                MODE_FUZZY,
                MODE_FLOAT_LIST,
                MODE_EXPLORER,
                MODE_COMPLETION,
                MODE_KEY_HINTS,
                MODE_ALERT,
                MODE_TERMINAL,
            }
            if self._line_is_selected(line_index):
                base_style = self.theme.ui_style("selection")
            elif is_current:
                base_style = self.theme.ui_style("cursor_line")
            else:
                base_style = self.theme.ui_style("editor")

            search_query = self._incremental_search_query if self.mode == MODE_COMMAND and self._command_prompt == "/" else ""
            colored_code = self._highlight_code_with_search(
                visible_code,
                base_style,
                search_query,
                line_index=line_index,
                line_text=raw,
                start_display=start_display,
            )
            if ghost_visible:
                ghost_style = self.theme.ui_style("message_info") or base_style
                colored = f"{colored_code}{ghost_style}{ghost_visible}{base_style}"
            else:
                colored = colored_code
            if gutter > 0:
                marker = " "
                if line_index in self._fold_collapsed:
                    marker = "▸" if self._unicode_ui else ">"
                elif line_index in self._active_pair_line_markers():
                    marker = "•"
                elif self._git_control_feature.enabled:
                    marker = self._git_control_feature.line_markers.get(line_index + 1, " ")
                    marker = marker if marker.strip() else " "
                number_width = max(1, gutter - 2)
                if self._soft_wrap_enabled and start_display > 0:
                    number = f"{'':>{number_width}}>{marker}"
                else:
                    number = f"{line_index + 1:>{number_width}}{marker} "
                number_style = (
                    self.theme.ui_style("line_number_current")
                    if is_current
                    else self.theme.ui_style("line_number")
                )
                return f"{number_style}{number}{colored}{base_style}{padding}{RESET}"

            return f"{colored}{base_style}{padding}{RESET}"

        filler = "~"
        text = f"{filler}{' ' * max(0, text_cols - 1)}"
        if gutter > 0:
            return (
                f"{self.theme.ui_style('line_number')}{' ' * gutter}"
                f"{self.theme.ui_style('tilde')}{text}{RESET}"
            )
        return f"{self.theme.ui_style('tilde')}{text}{RESET}"

    def _render_editor_row_for_view(
        self,
        screen_row: int,
        text_rows: int,
        gutter: int,
        text_cols: int,
        view_state: tuple[int, int, int, int],
    ) -> str:
        saved = self._capture_view_state()
        try:
            self._apply_view_state(view_state)
            return self._render_editor_row(screen_row, text_rows, gutter, text_cols)
        finally:
            self._apply_view_state(saved)

    def _render_terminal_row(self, screen_row: int, text_rows: int, width: int) -> str:
        base_style = self.theme.ui_style("editor")
        border_style = self.theme.ui_style("command_line")
        top_left, top_right, bottom_left, bottom_right, vertical, horizontal = self._box_chars()
        sessions = self._visible_terminal_sessions()
        if width <= 2:
            return f"{base_style}{' ' * max(0, width)}{RESET}"
        if screen_row == 0:
            if not sessions:
                title = " Terminal [idle] Esc close Ctrl+Q stop "
            elif len(sessions) == 1:
                active = sessions[0]
                title = f" Terminal [{active.process_id}] Esc close Ctrl+Q stop "
            else:
                active = sessions[0]
                peer = sessions[1]
                title = (
                    f" Terminal Split active={active.process_id} peer={peer.process_id} "
                    "Esc close Ctrl+Q stop Ctrl+W switch "
                )
            head = pad_to_display(slice_by_display(title, 0, width - 2), width - 2)
            return f"{border_style}{top_left}{head}{top_right}{RESET}"
        if screen_row == text_rows - 1:
            return f"{border_style}{bottom_left}{(horizontal * (width - 2))}{bottom_right}{RESET}"
        body_rows = max(1, text_rows - 2)
        if not sessions:
            visible = ["(terminal is idle)"]
        elif len(sessions) == 1:
            visible = self._terminal_session_rows(sessions[0], body_rows)
        else:
            top_rows = max(2, body_rows // 2)
            bottom_rows = max(2, body_rows - top_rows)
            if top_rows + bottom_rows > body_rows:
                bottom_rows = max(1, body_rows - top_rows)
            top_lines = self._terminal_session_rows(sessions[0], top_rows)
            bottom_lines = self._terminal_session_rows(sessions[1], bottom_rows)
            visible = [*top_lines, *bottom_lines]
        index = screen_row - 1
        content = visible[index] if 0 <= index < len(visible) else ""
        text = pad_to_display(slice_by_display(content, 0, width - 2), width - 2)
        return f"{base_style}{vertical}{text}{vertical}{RESET}"

    def _should_render_dashboard(self) -> bool:
        if self.file_path is not None:
            return False
        if self.modified or self.lines != [""]:
            return False
        return self.mode == MODE_NORMAL

    def _render_dashboard_row(self, screen_row: int, text_rows: int, gutter: int, text_cols: int) -> str:
        base_style = self.theme.ui_style("editor")
        title_style = self.theme.ui_style("mode_normal")
        accent_style = self.theme.ui_style("message_info")
        hints = [
            f"{APP_NAME} {APP_VERSION}",
            "",
            "~",
            ":e FILE        open file",
            ":project DIR   open folder",
            "i              insert mode",
            ":help          command help",
            "F1             key hints",
        ]
        block_height = len(hints)
        start_row = max(0, (text_rows - block_height) // 2)

        line = " " * text_cols
        style = base_style
        if start_row <= screen_row < start_row + block_height:
            local = screen_row - start_row
            text = hints[local]
            left_pad = max(0, (text_cols - display_width(text)) // 2)
            line = pad_to_display((" " * left_pad) + text, text_cols)
            if local == 0:
                style = title_style
            elif local == 2:
                style = accent_style
        if gutter > 0:
            return f"{self.theme.ui_style('line_number')}{' ' * gutter}{style}{line}{RESET}"
        return f"{style}{line}{RESET}"

    def _current_relative_path(self) -> Path | None:
        if self.file_path is None:
            return None
        try:
            return self.file_path.resolve().relative_to(self._workspace_root.resolve())
        except ValueError:
            return None

    def _build_sidebar_entries(self, files: list[Path]) -> list[tuple[str, Path | None]]:
        if not files:
            return []
        tree_entries = self._file_tree_feature._flatten_as_tree([str(item).replace("\\", "/") for item in files])
        entries: list[tuple[str, Path | None]] = []
        for entry in tree_entries:
            relative = Path(entry.relative_path) if entry.relative_path else None
            entries.append((entry.display, relative))
        return entries

    def _sidebar_start_index(self, entries: list[tuple[str, Path | None]], text_rows: int) -> int:
        if not entries:
            return 0

        current = self._current_relative_path()
        current_index = 0
        if current is not None:
            for index, (_display, relative) in enumerate(entries):
                if relative == current:
                    current_index = index
                    break

        visible = max(1, text_rows - 1)
        max_start = max(0, len(entries) - visible)
        return clamp(current_index - visible // 2, 0, max_start)

    def _render_sidebar_row(
        self,
        screen_row: int,
        text_rows: int,
        width: int,
        entries: list[tuple[str, Path | None]],
        start_index: int,
    ) -> str:
        if width <= 0:
            return ""

        base_style = self.theme.ui_style("sidebar")
        header_style = self.theme.ui_style("sidebar_header")
        current_style = self.theme.ui_style("sidebar_current")

        if screen_row == 0:
            file_count = sum(1 for _display, relative in entries if relative is not None)
            title = f" Files ({file_count}) "
            return f"{header_style}{pad_to_display(slice_by_display(title, 0, width), width)}{RESET}"

        file_index = start_index + screen_row - 1
        if file_index < len(entries):
            display, relative = entries[file_index]
            marker = " "
            if relative is not None and self.config.feature_enabled("git_status"):
                marker = self.git.status_for_relative(relative) or " "
            body = f"{marker} {display}" if relative is not None else f"  {display}"
            text = pad_to_display(slice_by_display(body, 0, width), width)
            is_current = relative is not None and self._current_relative_path() == relative
            style = current_style if is_current else base_style
            return f"{style}{text}{RESET}"

        return f"{base_style}{' ' * width}{RESET}"

    def _render_status_row(self, width: int) -> str:
        file_name = str(self.file_path) if self.file_path else "[No Name]"
        git_label = (
            self._git_control_feature.branch
            if self._git_control_feature.enabled
            else (self.git.branch_label(self.file_path) if self.config.feature_enabled("git_status") else "-")
        )
        context = LayoutContext(
            width=width,
            height=1,
            mode=self.mode,
            file_name=file_name,
            row=self.cy + 1,
            col=display_width(self._line()[: self.cx]) + 1,
            branch=git_label,
        )
        text = self._layout_manager.render_statusline(context)
        return f"{self.theme.ui_style('status')}{text}{RESET}"

    def _render_bottom_row(self, width: int) -> tuple[str, int]:
        if self.mode == MODE_COMMAND:
            if self._command_prompt == "/" and self.command_text.startswith("find "):
                shown = self.command_text[5:]
            else:
                shown = self.command_text
            text = self._command_prompt + shown
            text_width = display_width(text)
            if text_width <= width:
                visible = pad_to_display(text, width)
                cursor_col = text_width + 1
            else:
                visible = pad_to_display(slice_by_display(text, text_width - width, width), width)
                cursor_col = width
            return f"{self.theme.ui_style('command_line')}{visible}{RESET}", clamp(cursor_col, 1, width)

        if self.mode == MODE_TERMINAL:
            active = self._active_terminal_session()
            prefix = f"term[{active.process_id}]> " if active is not None else "term> "
            text = f"{prefix}{self._terminal_input}"
            text_width = display_width(text)
            if text_width <= width:
                visible = pad_to_display(text, width)
                cursor_col = text_width + 1
            else:
                visible = pad_to_display(slice_by_display(text, text_width - width, width), width)
                cursor_col = width
            return f"{self.theme.ui_style('command_line')}{visible}{RESET}", clamp(cursor_col, 1, width)

        if self.mode == MODE_FUZZY:
            text = f"fuzzy> {self.fuzzy_query}"
            text_width = display_width(text)
            if text_width <= width:
                visible = pad_to_display(text, width)
                cursor_col = text_width + 1
            else:
                visible = pad_to_display(slice_by_display(text, text_width - width, width), width)
                cursor_col = width
            return f"{self.theme.ui_style('command_line')}{visible}{RESET}", clamp(cursor_col, 1, width)

        if self.mode == MODE_FLOAT_LIST:
            footer = self._floating_list.footer if self._floating_list is not None else "Floating list"
            return f"{self.theme.ui_style('command_line')}{footer[:width].ljust(width)}{RESET}", 1

        if self.mode == MODE_EXPLORER:
            text = "Explorer: Enter open, Esc close, :tree refresh, :tree status"
            return f"{self.theme.ui_style('command_line')}{text[:width].ljust(width)}{RESET}", 1

        if self.mode == MODE_COMPLETION:
            text = "Completion: Tab/Enter accept, Esc close"
            return f"{self.theme.ui_style('command_line')}{text[:width].ljust(width)}{RESET}", 1

        if self.mode == MODE_KEY_HINTS:
            text = "Hints: Up/Down scroll, Esc close"
            return f"{self.theme.ui_style('command_line')}{text[:width].ljust(width)}{RESET}", 1

        if self.mode == MODE_ALERT:
            text = "Error popup: Esc/Enter close"
            return f"{self.theme.ui_style('command_line')}{text[:width].ljust(width)}{RESET}", 1

        line = pad_to_display(slice_by_display(self.message, 0, width), width)
        style = self.theme.ui_style("message_error") if self.message_error else self.theme.ui_style("message_info")
        return f"{style}{line}{RESET}", 1

    def _build_frame(self) -> tuple[list[str], int, int]:
        width, height = self._terminal_size()
        self._ensure_cursor_bounds()
        if self.mode in {MODE_NORMAL, MODE_INSERT, MODE_VISUAL}:
            self._active_bracket_pair = self._active_bracket_pair_under_cursor()
        else:
            self._active_bracket_pair = None

        plan = self._layout_manager.plan(width, height)
        sidebar_width = self._active_sidebar_width(width)
        self._ensure_cursor_visible(width, plan.editor_height + 2, sidebar_width)

        text_rows = max(1, plan.editor_height)
        gutter = self._gutter_width()
        editor_width = max(1, width - sidebar_width)
        text_cols = max(1, editor_width - gutter)
        split_render = self._split_enabled and self.mode in {MODE_NORMAL, MODE_INSERT, MODE_VISUAL}
        split_sizes = self._split_vertical_sizes(editor_width) if split_render and self._split_orientation == "vertical" else None
        if split_render and self._split_orientation == "vertical" and split_sizes is None:
            split_render = False
        self._capture_split_active_view()

        sidebar_entries = self._build_sidebar_entries(self.file_index.list_files()) if sidebar_width > 0 else []
        sidebar_start = self._sidebar_start_index(sidebar_entries, text_rows) if sidebar_entries else 0

        frame: list[str] = []
        file_name = str(self.file_path) if self.file_path else "[No Name]"
        layout_context = LayoutContext(
            width=width,
            height=height,
            mode=self.mode,
            file_name=file_name,
            row=self.cy + 1,
            col=display_width(self._line()[: self.cx]) + 1,
            branch=self._git_control_feature.branch
            if self._git_control_feature.enabled
            else (self.git.branch_label(self.file_path) if self.config.feature_enabled("git_status") else "-"),
        )
        if plan.tabline_height:
            separator = " │ " if self._unicode_ui else " | "
            tabline = self._layout_manager.render_tabline(
                layout_context,
                self._tab_items or [file_name],
                self._current_tab_index,
                separator=separator,
            )
            frame.append(f"{self.theme.ui_style('command_line')}{tabline}{RESET}")
        if plan.winbar_height:
            breadcrumb = self._build_winbar_breadcrumb()
            winbar = self._layout_manager.render_winbar(layout_context, breadcrumb)
            frame.append(f"{self.theme.ui_style('command_line')}{winbar}{RESET}")

        for screen_row in range(text_rows):
            if split_render:
                divider_style = self.theme.ui_style("command_line")
                divider_char = "│" if self._unicode_ui else "|"
                if self._split_orientation == "vertical":
                    assert split_sizes is not None
                    left_width, right_width = split_sizes
                    left_text_cols = max(1, left_width - gutter)
                    right_text_cols = max(1, right_width - gutter)
                    left_row = self._render_editor_row_for_view(
                        screen_row,
                        text_rows,
                        gutter,
                        left_text_cols,
                        self._split_main_view,
                    )
                    right_row = self._render_editor_row_for_view(
                        screen_row,
                        text_rows,
                        gutter,
                        right_text_cols,
                        self._split_secondary_view,
                    )
                    editor_row = f"{left_row}{divider_style}{divider_char}{RESET}{right_row}"
                else:
                    top_rows = max(2, (text_rows - 1) // 2)
                    bottom_rows = max(1, text_rows - top_rows - 1)
                    if screen_row < top_rows:
                        editor_row = self._render_editor_row_for_view(
                            screen_row,
                            top_rows,
                            gutter,
                            text_cols,
                            self._split_main_view,
                        )
                    elif screen_row == top_rows:
                        divider = "─" if self._unicode_ui else "-"
                        editor_row = f"{self.theme.ui_style('command_line')}{divider * editor_width}{RESET}"
                    else:
                        editor_row = self._render_editor_row_for_view(
                            screen_row - top_rows - 1,
                            bottom_rows,
                            gutter,
                            text_cols,
                            self._split_secondary_view,
                        )
            else:
                editor_row = self._render_editor_row(screen_row, text_rows, gutter, text_cols)
            if sidebar_width > 0:
                side = self._render_sidebar_row(
                    screen_row,
                    text_rows,
                    sidebar_width,
                    sidebar_entries,
                    sidebar_start,
                )
                frame.append(f"{side}{editor_row}")
            else:
                frame.append(editor_row)

        frame.append(self._render_status_row(width))
        bottom_row, command_cursor_col = self._render_bottom_row(width)
        frame.append(bottom_row)
        self._apply_notification_overlay(frame, width)

        if self.mode in {MODE_COMMAND, MODE_FUZZY, MODE_TERMINAL}:
            cursor_row = height
            cursor_col = command_cursor_col
        elif self.mode in {MODE_FLOAT_LIST, MODE_EXPLORER, MODE_COMPLETION, MODE_KEY_HINTS, MODE_ALERT}:
            cursor_row = height
            cursor_col = 1
        elif split_render:
            line = self._line()
            cursor_display = display_width(line[: self.cx])
            if self._split_orientation == "vertical":
                assert split_sizes is not None
                left_width, right_width = split_sizes
                pane_width = left_width if self._split_focus == "main" else right_width
                pane_text_cols = max(1, pane_width - gutter)
                pane_left = sidebar_width + (0 if self._split_focus == "main" else left_width + 1)
                if self._soft_wrap_enabled:
                    visual_row = self._cursor_softwrap_row(pane_text_cols)
                    cursor_row = plan.editor_top + clamp(visual_row, 1, text_rows)
                    segment_start = (cursor_display // pane_text_cols) * pane_text_cols
                    cursor_col = clamp(pane_left + gutter + (cursor_display - segment_start) + 1, 1, width)
                else:
                    cursor_row = plan.editor_top + clamp(self.cy - self.row_offset + 1, 1, text_rows)
                    cursor_col = clamp(pane_left + gutter + (cursor_display - self.col_offset) + 1, 1, width)
            else:
                top_rows = max(2, (text_rows - 1) // 2)
                bottom_rows = max(1, text_rows - top_rows - 1)
                pane_rows = top_rows if self._split_focus == "main" else bottom_rows
                pane_top = plan.editor_top + (0 if self._split_focus == "main" else top_rows + 1)
                pane_text_cols = max(1, text_cols)
                if self._soft_wrap_enabled:
                    visual_row = self._cursor_softwrap_row(pane_text_cols)
                    cursor_row = pane_top + clamp(visual_row, 1, pane_rows)
                    segment_start = (cursor_display // pane_text_cols) * pane_text_cols
                    cursor_col = clamp(sidebar_width + gutter + (cursor_display - segment_start) + 1, 1, width)
                else:
                    cursor_row = pane_top + clamp(self.cy - self.row_offset + 1, 1, pane_rows)
                    cursor_col = clamp(sidebar_width + gutter + (cursor_display - self.col_offset) + 1, 1, width)
        else:
            line = self._line()
            cursor_display = display_width(line[: self.cx])
            if self._soft_wrap_enabled:
                visual_row = self._cursor_softwrap_row(text_cols)
                cursor_row = plan.editor_top + clamp(visual_row, 1, text_rows)
                segment_start = (cursor_display // max(1, text_cols)) * max(1, text_cols)
                cursor_col = clamp(sidebar_width + gutter + (cursor_display - segment_start) + 1, 1, width)
            else:
                cursor_row = plan.editor_top + clamp(self.cy - self.row_offset + 1, 1, text_rows)
                cursor_col = clamp(sidebar_width + gutter + (cursor_display - self.col_offset) + 1, 1, width)

        return frame, cursor_row, cursor_col

    def _build_winbar_breadcrumb(self) -> str:
        if self.file_path is None:
            return "[No Name]"
        parts: list[str] = []
        parent_name = self.file_path.parent.name
        if parent_name:
            parts.append(parent_name)
        parts.append(self.file_path.name)
        service = self._get_ast_query_service()
        if service is not None:
            try:
                node = service.node_at(self.file_path, self.lines, self.cy + 1, self.cx + 1)
            except Exception:
                node = None
            if node and node.name:
                suffix = "()" if node.kind in {"function", "method"} else ""
                parts.append(f"{node.name}{suffix}")
        return " > ".join(parts[-3:])

    def _apply_notification_overlay(self, frame: list[str], width: int) -> None:
        if not self.config.feature_enabled("notifications"):
            return
        items = self._notifications.active()
        if not items:
            return
        visible = items[-2:]
        title = " Notifications "
        top_left, top_right, bottom_left, bottom_right, vertical, horizontal = self._box_chars()
        content_width = max(display_width(title), *(display_width(item) for item in visible))
        box_width = clamp(content_width + 2, 20, max(20, width))
        if box_width >= width:
            box_width = width
        left = max(0, width - box_width)

        header = pad_to_display(slice_by_display(title, 0, max(0, box_width - 2)), max(0, box_width - 2))
        top = top_left + header + top_right
        bottom = bottom_left + (horizontal * max(0, box_width - 2)) + bottom_right
        rows = [top]
        for item in visible:
            content = pad_to_display(slice_by_display(item, 0, max(0, box_width - 2)), max(0, box_width - 2))
            rows.append(vertical + content + vertical)
        rows.append(bottom)

        start = max(0, len(frame) - len(rows) - 1)
        style = self.theme.ui_style("command_line")
        for index, row_text in enumerate(rows):
            row_index = start + index
            if row_index >= len(frame):
                break
            full = (" " * left) + row_text
            frame[row_index] = f"{style}{full[:width].ljust(width)}{RESET}"

    def render(self) -> None:
        frame, cursor_row, cursor_col = self._build_frame()

        if len(self._last_frame) != len(frame):
            self._last_frame = [""] * len(frame)

        width, height = self._terminal_size()
        plan = self._layout_manager.plan(width, height)
        view_state = (
            self.row_offset,
            self.col_offset,
            self.mode,
            self._soft_wrap_enabled,
            self._active_sidebar_width(width),
            self._gutter_width(),
            frozenset(self._feature_registry.enabled_components()),
        )
        dirty_all, dirty_lines = self.buffer.consume_dirty()
        text_rows = max(1, plan.editor_height)
        editor_top = plan.editor_top

        if (
            dirty_all
            or self._last_view_state != view_state
            or self.mode in {
                MODE_FUZZY,
                MODE_FLOAT_LIST,
                MODE_EXPLORER,
                MODE_COMPLETION,
                MODE_KEY_HINTS,
                MODE_ALERT,
                MODE_TERMINAL,
            }
        ):
            candidate_rows = set(range(1, len(frame) + 1))
        else:
            candidate_rows = {len(frame) - 1, len(frame)}
            for line_index in dirty_lines:
                screen_row = line_index - self.row_offset + 1 + editor_top
                if editor_top < screen_row <= editor_top + text_rows:
                    candidate_rows.add(screen_row)

            previous_screen_row = self._last_cursor_line - self.row_offset + 1 + editor_top
            current_screen_row = self.cy - self.row_offset + 1 + editor_top
            if editor_top < previous_screen_row <= editor_top + text_rows:
                candidate_rows.add(previous_screen_row)
            if editor_top < current_screen_row <= editor_top + text_rows:
                candidate_rows.add(current_screen_row)

        for row_index, line in enumerate(frame, start=1):
            if line != self._last_frame[row_index - 1]:
                candidate_rows.add(row_index)

        dirty_rows: list[int] = []
        for row in sorted(candidate_rows):
            line = frame[row - 1]
            if line != self._last_frame[row - 1]:
                dirty_rows.append(row)

        self._ui.update_grid(frame, dirty_rows=dirty_rows)
        self._ui.set_cursor(cursor_row, cursor_col)
        self._ui.flush()
        self._last_frame = frame
        self._last_view_state = view_state
        self._last_cursor_line = self.cy

    def _shortcut(self, action: str, default: str) -> str:
        language = self._language_id_for_file(self.file_path).lower()
        return self.config.shortcut_for_language(action, default, language)

    def _handle_shortcuts(self, key: str) -> bool:
        if key == self.key_hints_trigger:
            if self.mode == MODE_KEY_HINTS:
                self.mode = MODE_NORMAL
            else:
                self._open_key_hints()
            return True

        if not self.config.shortcuts_enabled():
            return False

        if key == self._shortcut("toggle_comment", "CTRL_SLASH"):
            if self.mode != MODE_FUZZY:
                self._toggle_comment()
                return True
            return False

        if key == self._shortcut("add_cursor_down", "CTRL_D"):
            if self.mode in {MODE_NORMAL, MODE_INSERT, MODE_VISUAL}:
                if not self._add_cursor_next_match():
                    self._add_cursor_down()
                return True
            return False

        if key == self._shortcut("clear_multi_cursor", "CTRL_U"):
            self._clear_multi_cursor()
            self._set_message("Multi-cursor cleared.")
            return True

        if key == self._shortcut("word_left", "CTRL_LEFT"):
            if self.mode in {MODE_NORMAL, MODE_INSERT, MODE_VISUAL}:
                self._move_word_left()
                return True
            return False

        if key == self._shortcut("word_right", "CTRL_RIGHT"):
            if self.mode in {MODE_NORMAL, MODE_INSERT, MODE_VISUAL}:
                self._move_word_right()
                return True
            return False

        if key == self._shortcut("quick_find", "CTRL_F"):
            if self.mode != MODE_COMMAND:
                self._enter_command("find ")
                return True
            return False

        if key == self._shortcut("quick_replace", "CTRL_G"):
            if self.mode != MODE_COMMAND:
                self._enter_command("replace ")
                return True
            return False

        if key == self._shortcut("fuzzy_finder", "CTRL_P"):
            if self.mode != MODE_COMMAND:
                self._open_fuzzy()
                return True
            return False

        if key == self._shortcut("jump_back", "CTRL_O"):
            if self.mode in {MODE_NORMAL, MODE_INSERT, MODE_VISUAL}:
                self._jump_back()
                return True
            return False

        if key == self._shortcut("toggle_sidebar", "F4"):
            self._toggle_sidebar()
            return True

        if key == self._shortcut("toggle_file_tree", "F3"):
            if self.mode == MODE_EXPLORER:
                self._close_explorer()
            else:
                self._open_explorer(refresh=False)
            return True

        if key == self._shortcut("open_completion", "CTRL_N"):
            if self.mode == MODE_INSERT:
                return self._open_tab_completion()
            return False

        if key == self._shortcut("format_code", "F8"):
            self._format_code()
            return True

        if key == self._shortcut("refactor_rename", "F6"):
            if self.mode != MODE_COMMAND:
                self._open_rename_prompt()
                return True
            return False

        return False

    def _read_key(self) -> str:
        if self._input_queue:
            return self._input_queue.popleft()
        return KeyReader.read_key()

    def _dispatch_plugin_key(self, key: str) -> None:
        if not self.config.feature_enabled("plugins"):
            return
        if not self.config.feature_enabled("plugin_keyhooks"):
            return
        if self.mode == MODE_INSERT and len(key) == 1 and key.isprintable():
            return
        self._ensure_plugins_loaded()
        responses = self.plugins.run_on_key(key)
        if not responses:
            return
        latest = responses[-1]
        if "line " in latest.lower():
            self._show_alert(f"Plugin runtime error: {latest}")
            return
        self._set_message(latest)

    def handle_key(self, key: str) -> None:
        before = self._capture_snapshot()
        self._record_key_for_macro(key)
        try:
            if self.mode == MODE_ALERT:
                self._handle_alert_key(key)
                return

            if self.mode == MODE_KEY_HINTS:
                self._handle_key_hints_key(key)
                return

            if self.mode == MODE_FLOAT_LIST:
                self._handle_floating_list_key(key)
                return

            if self.mode == MODE_EXPLORER:
                self._handle_explorer_key(key)
                return

            if self.mode == MODE_COMPLETION:
                self._handle_completion_key(key)
                return

            if self.mode == MODE_TERMINAL:
                self._handle_terminal_key(key)
                return

            if key == "CTRL_S":
                self.save_file()
                return

            if key in {"CTRL_Q", "CTRL_C"}:
                self.request_quit(force=False)
                return

            if self._handle_shortcuts(key):
                return

            if self.mode == MODE_COMMAND:
                self._handle_command_key(key)
                self._dispatch_plugin_key(key)
                return

            if self.mode == MODE_FUZZY:
                self._handle_fuzzy_key(key)
                self._dispatch_plugin_key(key)
                return

            if self.mode == MODE_VISUAL:
                self._handle_visual_key(key)
                self._dispatch_plugin_key(key)
                return

            if self.mode == MODE_INSERT:
                self._handle_insert_key(key)
                self._dispatch_plugin_key(key)
                return

            self._handle_normal_key(key)
            self._dispatch_plugin_key(key)
        except Exception as exc:
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            self._set_message(self._friendly_error_message(exc), error=True)
        finally:
            self._capture_split_active_view()
            self._push_history_if_changed(before, label=f"key:{key}")

    def run(self) -> None:
        if isinstance(self._ui, TerminalUI):
            self._ui.enter()
            self._ui_entered = True
        try:
            while self.running:
                self._poll_config_reload()
                if self._input_queue or KeyReader.has_key():
                    from_queue = bool(self._input_queue)
                    key = self._read_key()
                    self._macro_replaying = from_queue
                    self.handle_key(key)
                    self._macro_replaying = False
                    self.render()
                    self._drain_async_events(max_items=8)
                    self._schedule_git_control_refresh()
                    if self.mode == MODE_EXPLORER and not self._file_tree_feature.entries:
                        self._schedule_file_tree_refresh()
                    self._write_swap_if_needed()
                    self._auto_save_if_needed()
                    self._last_tick = time.monotonic()
                    continue
                self._drain_async_events()
                self._schedule_git_control_refresh()
                if self.mode == MODE_EXPLORER and not self._file_tree_feature.entries:
                    self._schedule_file_tree_refresh()
                self._write_swap_if_needed()
                self._auto_save_if_needed()
                self.render()
                time.sleep(0.01)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self.running = False
        self._save_session()
        self._write_swap_if_needed(force=True)
        if hasattr(self._process_manager, "stop_all_sync"):
            self._process_manager.stop_all_sync(timeout=1.2)
        else:
            for process_id in list(self._terminal_sessions):
                self._process_manager.stop_sync(process_id, timeout=1.2)
        self._terminal_sessions.clear()
        self._terminal_session_order.clear()
        self._terminal_process_id = None
        if self._lsp_client is not None:
            try:
                self._async_runtime.run_sync(self._lsp_client.stop(), timeout=2.0)
            except Exception:
                pass
        self._async_runtime.close()
        if self._ui_entered and isinstance(self._ui, TerminalUI):
            self._ui.exit()
            self._ui_entered = False


# Backward-compat alias for older imports.
PviEditor = PvimEditor
