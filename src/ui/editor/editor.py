from __future__ import annotations

import asyncio
import cProfile
from collections import deque
import io
import json
import os
from pathlib import Path
import pstats
import re
import sys
import time

from ... import APP_NAME, APP_VERSION
from ...core.async_runtime import AsyncRuntime
from ...core.buffer import Buffer
from ...core.config import AppConfig
from ...core.console import CSI, ConsoleController, KeyReader
from ...core.display import display_width, index_from_display_col, pad_to_display, slice_by_display
from ...core.history import ActionRecord, ActionSnapshot, HistoryStack
from ...core.persistence import EditorPersistence, SwapPayload
from ...core.process_pipe import AsyncProcessManager
from ...core.terminal_capabilities import detect_terminal_capabilities
from ...core.theme import RESET, Theme, load_theme
from ...features.ast_query import AstQueryService
from ...features.file_index import FileIndex
from ...features.formatter import normalize_code_style, organize_python_imports
from ...features.fuzzy import fuzzy_filter
from ...features.git_status import GitStatusProvider
from ...features.live_grep import GrepMatch, LiveGrep
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
    MODE_VISUAL,
)
from .normal_mode import NormalModeMixin
from .text_objects import is_word_char, quote_range, word_range
from .ui_mode import UIModeMixin


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class PvimEditor(NormalModeMixin, InsertModeMixin, UIModeMixin, CommandsMixin):
    def __init__(self, file_path: Path | None, config: AppConfig) -> None:
        self.config = config

        self.buffer = Buffer(lines=[""])
        self.file_path: Path | None = None

        self.cx = 0
        self.cy = 0
        self.row_offset = 0
        self.col_offset = 0

        self.mode = MODE_NORMAL
        self.pending_operator = ""
        self.pending_scope = ""
        self._pending_motion = ""
        self.command_text = ""
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

        self._history = HistoryStack(max_actions=400)
        self._history_enabled = True
        self._history_suspended = False
        self._skip_history_once = False
        self._current_line_ending = "\n"

        self._input_queue: deque[str] = deque()
        self._macro_registers: dict[str, list[str]] = {}
        self._macro_recording_register: str | None = None
        self._macro_recording_keys: list[str] = []
        self._macro_waiting_action: str = ""
        self._macro_replaying = False

        self._persistence = EditorPersistence()
        self._swap_enabled = True
        self._swap_interval = 4.0
        self._last_swap_write = 0.0
        self._swap_prompt_path: Path | None = None
        self._swap_prompt_payload: SwapPayload | None = None
        self._session_enabled = True
        self._session_path = Path.cwd() / ".pvim.session.json"
        self._soft_wrap_enabled = True

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
        self._console = ConsoleController()
        self._async_runtime = AsyncRuntime()
        self._process_manager = AsyncProcessManager(self._async_runtime)
        self._last_tick = time.monotonic()
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
        self.sidebar_width = 30
        self._lazy_load_enabled = True
        self._terminal_capabilities = detect_terminal_capabilities()
        self._unicode_ui = self._terminal_capabilities.unicode_ui

        self.theme: Theme = load_theme(None, self._terminal_capabilities)
        self.syntax: SyntaxManager | None = None
        self._syntax_profile = PLAIN_PROFILE
        self.file_index = FileIndex(Path.cwd(), max_files=3000)
        self.git = GitStatusProvider(Path.cwd(), enabled=False, refresh_seconds=2.0)
        self._ast_query_service: AstQueryService | None = None
        self._file_tree_feature = FileTreeFeature(enabled=False)
        self._completion_feature = TabCompletionFeature(enabled=False)
        self._git_control_feature = GitControlFeature(enabled=False)
        self.plugins = PluginManager(
            plugins_root=Path.cwd() / "plugins",
            enabled=False,
            step_limit=100_000,
            auto_load=False,
            host_api=self._plugin_api_dispatch,
        )
        self._plugins_loaded = False
        self._auto_pairs: dict[str, str] = {}
        self._auto_pair_closers: set[str] = set()
        self._register_feature_descriptors()

        self._apply_runtime_config()

        if file_path is not None:
            self.open_file(file_path, force=True, startup=True)
        elif self._session_enabled:
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
        return f"utf-8 Ln {context.row}, Col {context.col}"

    def _apply_runtime_config(self) -> None:
        self.show_line_numbers = self.config.show_line_numbers()
        self.tab_size = self.config.tab_size()
        self._soft_wrap_enabled = self.config.soft_wrap_enabled()
        self.show_sidebar = self.config.sidebar_enabled()
        self.sidebar_width = self.config.sidebar_width()
        self.key_hints_enabled = self.config.key_hints_enabled()
        self.key_hints_trigger = self.config.key_hints_trigger()
        self._lazy_load_enabled = self.config.lazy_load_enabled()
        self._swap_enabled = self.config.swap_enabled()
        self._swap_interval = self.config.swap_interval_seconds()
        self._session_enabled = self.config.session_enabled()
        self._session_path = self.config.session_file()
        self._history_enabled = self.config.undo_tree_enabled()
        self._history.set_limit(self.config.undo_tree_max_actions())
        self._feature_registry.set_enabled("tabline", self.config.feature_enabled("tabline"))
        self._feature_registry.set_enabled("winbar", self.config.feature_enabled("winbar"))
        self._feature_registry.set_enabled("file_tree", self.config.feature_enabled("file_tree"))
        self._feature_registry.set_enabled("tab_completion", self.config.feature_enabled("tab_completion"))
        self._feature_registry.set_enabled("git_control", self.config.feature_enabled("git_control"))
        self._feature_registry.set_enabled("notifications", self.config.feature_enabled("notifications"))
        self._file_tree_feature.enabled = self.config.feature_enabled("file_tree")
        self._completion_feature.enabled = self.config.feature_enabled("tab_completion")
        self._git_control_feature.enabled = self.config.feature_enabled("git_control")
        self._tab_items = [self.file_path.name] if self.file_path else ["[No Name]"]
        self._current_tab_index = 0
        if not self._file_tree_feature.enabled:
            self._close_explorer()
        if not self._completion_feature.enabled:
            self._close_tab_completion()
        if not self.config.feature_enabled("notifications"):
            self._notifications = NotificationCenter()

        theme_file = self.config.theme_file() if self.config.theme_enabled() else None
        self.theme = load_theme(theme_file, self._terminal_capabilities)
        if self._lazy_load_enabled:
            self.syntax = None
            self._syntax_profile = PLAIN_PROFILE
        else:
            self.syntax = SyntaxManager(self.config)
            self._syntax_profile = self.syntax.profile_for_file(self.file_path)

        self.file_index = FileIndex(Path.cwd(), max_files=self.config.file_scan_limit())
        self.git = GitStatusProvider(
            Path.cwd(),
            enabled=self.config.feature_enabled("git_status"),
            refresh_seconds=self.config.git_refresh_seconds(),
        )
        self.plugins = PluginManager(
            plugins_root=self.config.plugins_directory(),
            enabled=self.config.feature_enabled("plugins"),
            step_limit=self.config.script_step_limit(),
            auto_load=self.config.plugins_auto_load() and not self._lazy_load_enabled,
            host_api=self._plugin_api_dispatch,
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
        self._auto_pairs = self._load_auto_pairs()
        self._auto_pair_closers = set(self._auto_pairs.values())
        self._file_tree_feature.unicode_art = self._unicode_ui

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
            "  Ctrl+D add cursor down",
            "  Ctrl+U clear multi-cursor",
            "  Ctrl+Left / Ctrl+Right move by word",
            "  Tab / Shift+Tab indent / outdent",
            "  u undo / Ctrl+Y redo",
            "  q a ... q record macro / @a replay",
            "  ciw da\" vap cif text objects",
            "",
            "Tools",
            "  Ctrl+F quick find",
            "  Ctrl+G quick replace",
            "  Ctrl+N tab completion",
            "  Ctrl+P fuzzy finder",
            "  :grep <text> project search",
            "  Ctrl+R rename symbol",
            "  F3 file tree",
            "  F4 toggle sidebar",
            "  F8 normalize code style",
            "",
            "Plugin commands",
            "  :plugin list",
            "  :plugin load",
            "  :plugin install <path>",
            "  :plugin run <name> <function> [args ...]",
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
            "  :session save|load",
            "  :swap write|clear",
            "  :tree open|refresh|close|toggle",
            "  :feature <name> <on|off>",
        ]
        return lines

    def _set_message(self, message: str, *, error: bool = False) -> None:
        self.message = message
        self.message_error = error
        if self.config.feature_enabled("notifications") and message:
            ttl = 3.0 if error else 2.0
            self._notifications.push(message, ttl_seconds=ttl)

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

    def _start_macro_recording(self, register: str) -> None:
        self._macro_recording_register = register
        self._macro_recording_keys = []
        self._set_message(f"Recording macro @{register}")

    def _stop_macro_recording(self) -> None:
        register = self._macro_recording_register
        if register is None:
            return
        self._macro_registers[register] = list(self._macro_recording_keys)
        self._macro_recording_register = None
        self._macro_recording_keys = []
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

    def _normalize_loaded_text(self, text: str) -> tuple[str, str]:
        if "\r\n" in text:
            line_ending = "\r\n"
        elif "\r" in text:
            line_ending = "\r\n"
        else:
            line_ending = "\n"
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized, line_ending

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

    def _restore_session(self) -> None:
        data = self._persistence.load_session(self._session_path)
        if not data:
            return
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

    def _save_session(self) -> None:
        if not self._session_enabled:
            return
        payload = {
            "current_file": str(self.file_path) if self.file_path else "",
            "cursor_x": self.cx,
            "cursor_y": self.cy,
            "tabs": list(self._tab_items),
            "tab_index": self._current_tab_index,
        }
        try:
            self._persistence.save_session(self._session_path, payload)
        except OSError:
            return

    def _start_live_grep_task(self, version: int, query: str) -> None:
        self._live_grep_task_active = True
        self._live_grep_running_version = version

        async def _search() -> tuple[int, str, list[GrepMatch]]:
            matches = await self._live_grep.search(
                Path.cwd(),
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
                    if self._floating_source == "live_grep" and self._floating_list is not None:
                        labels = [match.label(Path.cwd()) for match in matches]
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
                    self._set_message(f"[proc:{process_id}] {line}")
                continue

            if event_type == "process_exit":
                process_id = int(event.get("process_id", 0))
                code = event.get("return_code", None)
                self._set_message(f"Process {process_id} exited ({code}).")

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

    def _syntax_manager(self) -> SyntaxManager:
        if self.syntax is None:
            self.syntax = SyntaxManager(self.config)
            self._syntax_profile = self.syntax.profile_for_file(self.file_path)
        return self.syntax

    def _start_file_tree_task(self, version: int) -> None:
        self._file_tree_task_active = True
        self._file_tree_running_version = version

        async def _collect() -> tuple[int, list[str]]:
            paths = await self._file_tree_feature.collect_paths(Path.cwd())
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
            snapshot = await self._git_control_feature.collect(Path.cwd(), target)
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

    def _resolve_path(self, target: Path | str) -> Path:
        path = Path(target).expanduser()
        if path.is_absolute():
            return path

        base = self.file_path.parent if self.file_path else Path.cwd()
        return (base / path).resolve()

    def _terminal_size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size()
            return max(40, size.columns), max(8, size.lines)
        except OSError:
            return 120, 30

    def _line(self) -> str:
        return self.lines[self.cy]

    def _set_line(self, value: str) -> None:
        self.lines[self.cy] = value
        self.buffer.mark_dirty(self.cy)

    def _mark_modified(self) -> None:
        self.modified = True
        self.pending_operator = ""
        self.buffer.mark_dirty(self.cy)

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

    def _active_sidebar_width(self, width: int) -> int:
        if not self.show_sidebar or not self.config.sidebar_enabled():
            return 0
        if self.mode in {MODE_FUZZY, MODE_FLOAT_LIST, MODE_EXPLORER, MODE_COMPLETION, MODE_KEY_HINTS, MODE_ALERT}:
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
        self._set_line(left)
        self.lines.insert(self.cy + 1, right)
        self.buffer.mark_all_dirty()
        self.cy += 1
        self.cx = 0
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
        self.cy += 1
        self.lines.insert(self.cy, "")
        self.buffer.mark_all_dirty()
        self.cx = 0
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

        return False

    def _goto_definition(self) -> None:
        symbol = word_at_cursor(self._line(), self.cx)
        if not symbol:
            self._set_message("No symbol under cursor.", error=True)
            return

        pattern = re.compile(rf"^\s*(def|class)\s+{re.escape(symbol)}\b")
        for index, line in enumerate(self.lines):
            if pattern.search(line):
                self.cy = index
                self.cx = max(0, line.find(symbol))
                self._set_message(f"Definition: {symbol} (current file)")
                return

        self.file_index.refresh()
        for relative in self.file_index.list_files()[:2000]:
            path = Path.cwd() / relative
            if self.file_path is not None and path.resolve() == self.file_path.resolve():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for index, line in enumerate(lines):
                if pattern.search(line):
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
        self.fuzzy_matches = fuzzy_filter(all_files, self.fuzzy_query, limit=40)
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
        target = Path.cwd() / self.fuzzy_matches[self.fuzzy_index]
        if self.open_file(target, force=False):
            self.mode = MODE_NORMAL
            self.fuzzy_query = ""
            self.fuzzy_matches = []
            self.fuzzy_index = 0
            self._floating_list = None
            self._floating_source = ""

    def _sync_file_tree_popup(self) -> None:
        entries = [entry.display for entry in self._file_tree_feature.entries]
        if not entries:
            entries = ["(loading...)" if self._file_tree_task_active else "(empty)"]
        popup = self._floating_list
        if popup is None or self._floating_source != "file_tree":
            popup = FloatingList(
                title="EXPLORER",
                footer="<Esc> close  <Enter> open  <Up/Down> move",
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
        self._completion_feature.open(prefix, self.lines, ast_hint)
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
            f"{item['name']} | loaded={item['loaded']} | {item['error'] or 'ok'}"
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
        self.show_sidebar = not self.show_sidebar
        state = "on" if self.show_sidebar else "off"
        self._set_message(f"Sidebar: {state}")

    def _find(self, query: str) -> bool:
        if not self.config.feature_enabled("find_replace"):
            self._set_message("Find/replace is disabled in config.", error=True)
            return False
        if not query:
            self._set_message("Usage: :find <text>", error=True)
            return False

        location = find_next(self.lines, query, self.cy, self.cx + 1)
        if location is None:
            self._set_message(f"Not found: {query}", error=True)
            return False

        self.cy, self.cx = location
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

    def _enter_command(self, initial: str = "") -> None:
        self.mode = MODE_COMMAND
        self.command_text = initial
        self.pending_operator = ""
        self.visual_anchor = None

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
            self._set_message("Usage: :plugin list|load|install|run ...", error=True)
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

        self._set_message("Usage: :plugin list|load|install|run ...", error=True)
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

    def open_file(self, target: Path | str, *, force: bool, startup: bool = False) -> bool:
        if self.modified and not force and not startup:
            self._set_message("Unsaved changes. Use :e! <file> to force.", error=True)
            return False

        path = self._resolve_path(target)
        text = ""
        if path.exists():
            try:
                raw = path.read_bytes().decode("utf-8")
                normalized, detected_line_ending = self._normalize_loaded_text(raw)
                text = normalized
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

        self.file_path = path
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
        self._syntax_profile = self._syntax_manager().profile_for_file(self.file_path)
        if self.file_path is not None:
            tab_label = self.file_path.name
            if tab_label not in self._tab_items:
                self._tab_items.append(tab_label)
            self._current_tab_index = self._tab_items.index(tab_label)
        self._schedule_git_control_refresh(force=True)
        self._maybe_prompt_swap_recovery(path)
        return True

    def save_file(self, target: Path | str | None = None) -> bool:
        if target is not None:
            self.file_path = self._resolve_path(target)

        if self.file_path is None:
            self._set_message("No file name. Use :w <path>.", error=True)
            return False

        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            current = self.buffer.text()
            target_newline = self._current_line_ending if self.config.preserve_line_ending() else self.config.default_line_ending()
            data = current.replace("\n", target_newline)
            self.file_path.write_text(data, encoding="utf-8", newline="")
        except OSError as exc:
            self._set_message(f"Write failed: {exc}", error=True)
            return False

        self.modified = False
        if self.file_path is not None:
            tab_label = self.file_path.name
            if tab_label not in self._tab_items:
                self._tab_items.append(tab_label)
            self._current_tab_index = self._tab_items.index(tab_label)
        self._schedule_git_control_refresh(force=True)
        if self.file_path is not None:
            self._persistence.remove_swap(self.file_path)
        self._set_message(f"Wrote {self.file_path}")
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
            }
            if self._line_is_selected(line_index):
                base_style = self.theme.ui_style("selection")
            elif is_current:
                base_style = self.theme.ui_style("cursor_line")
            else:
                base_style = self.theme.ui_style("editor")

            colored_code = self._syntax_manager().highlight_line(
                visible_code,
                self._syntax_profile,
                self.theme,
                base_style,
            )
            if ghost_visible:
                ghost_style = self.theme.ui_style("message_info") or base_style
                colored = f"{colored_code}{ghost_style}{ghost_visible}{base_style}"
            else:
                colored = colored_code
            if gutter > 0:
                marker = " "
                if self._git_control_feature.enabled:
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

    def _should_render_dashboard(self) -> bool:
        if self.file_path is not None:
            return False
        if self.modified or self.lines != [""]:
            return False
        return self.mode == MODE_NORMAL

    def _render_dashboard_row(self, screen_row: int, text_rows: int, gutter: int, text_cols: int) -> str:
        base_style = self.theme.ui_style("editor")
        accent_style = self.theme.ui_style("fuzzy_selected")
        hints = [
            APP_NAME,
            f"v{APP_VERSION}",
            "",
            "i        insert mode",
            ":e FILE  open file",
            ":help    command help",
            "F1       key hints",
        ]
        start = max(0, (text_rows - len(hints)) // 2)
        line = " " * text_cols
        style = base_style
        if start <= screen_row < start + len(hints):
            message = slice_by_display(hints[screen_row - start], 0, text_cols)
            message_width = display_width(message)
            left_pad = max(0, (text_cols - message_width) // 2)
            line = pad_to_display((" " * left_pad) + message, text_cols)
            if screen_row - start in {0, 1}:
                style = accent_style
        if gutter > 0:
            return f"{self.theme.ui_style('line_number')}{' ' * gutter}{style}{line}{RESET}"
        return f"{style}{line}{RESET}"

    def _current_relative_path(self) -> Path | None:
        if self.file_path is None:
            return None
        try:
            return self.file_path.resolve().relative_to(Path.cwd().resolve())
        except ValueError:
            return None

    def _sidebar_start_index(self, files: list[Path], text_rows: int) -> int:
        if not files:
            return 0

        current = self._current_relative_path()
        current_index = 0
        if current is not None:
            try:
                current_index = files.index(current)
            except ValueError:
                current_index = 0

        visible = max(1, text_rows - 1)
        max_start = max(0, len(files) - visible)
        return clamp(current_index - visible // 2, 0, max_start)

    def _render_sidebar_row(
        self,
        screen_row: int,
        text_rows: int,
        width: int,
        files: list[Path],
        start_index: int,
    ) -> str:
        if width <= 0:
            return ""

        base_style = self.theme.ui_style("sidebar")
        header_style = self.theme.ui_style("sidebar_header")
        current_style = self.theme.ui_style("sidebar_current")

        if screen_row == 0:
            title = f" Files ({len(files)}) "
            return f"{header_style}{pad_to_display(slice_by_display(title, 0, width), width)}{RESET}"

        file_index = start_index + screen_row - 1
        if file_index < len(files):
            relative = files[file_index]
            marker = self.git.status_for_relative(relative) if self.config.feature_enabled("git_status") else " "
            marker = marker if marker.strip() else " "
            body = f"{marker} {relative}"
            text = pad_to_display(slice_by_display(body, 0, width), width)
            is_current = self._current_relative_path() == relative
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
            text = ":" + self.command_text
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
            text = "Explorer: Enter open, Esc close, :tree refresh"
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

        plan = self._layout_manager.plan(width, height)
        sidebar_width = self._active_sidebar_width(width)
        self._ensure_cursor_visible(width, plan.editor_height + 2, sidebar_width)

        text_rows = max(1, plan.editor_height)
        gutter = self._gutter_width()
        editor_width = max(1, width - sidebar_width)
        text_cols = max(1, editor_width - gutter)

        sidebar_files = self.file_index.list_files() if sidebar_width > 0 else []
        sidebar_start = self._sidebar_start_index(sidebar_files, text_rows) if sidebar_files else 0

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
            editor_row = self._render_editor_row(screen_row, text_rows, gutter, text_cols)
            if sidebar_width > 0:
                side = self._render_sidebar_row(
                    screen_row,
                    text_rows,
                    sidebar_width,
                    sidebar_files,
                    sidebar_start,
                )
                frame.append(f"{side}{editor_row}")
            else:
                frame.append(editor_row)

        frame.append(self._render_status_row(width))
        bottom_row, command_cursor_col = self._render_bottom_row(width)
        frame.append(bottom_row)
        self._apply_notification_overlay(frame, width)

        if self.mode in {MODE_COMMAND, MODE_FUZZY}:
            cursor_row = height
            cursor_col = command_cursor_col
        elif self.mode in {MODE_FLOAT_LIST, MODE_EXPLORER, MODE_COMPLETION, MODE_KEY_HINTS, MODE_ALERT}:
            cursor_row = height
            cursor_col = 1
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

        out: list[str] = []
        for row in sorted(candidate_rows):
            line = frame[row - 1]
            if line != self._last_frame[row - 1]:
                out.append(f"{CSI}{row};1H{line}")

        out.append(f"{CSI}{cursor_row};{cursor_col}H")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._last_frame = frame
        self._last_view_state = view_state
        self._last_cursor_line = self.cy

    def _shortcut(self, action: str, default: str) -> str:
        return self.config.shortcut(action, default)

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

        if key == self._shortcut("refactor_rename", "CTRL_R"):
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
        finally:
            self._push_history_if_changed(before, label=f"key:{key}")

    def run(self) -> None:
        self._console.enter()
        try:
            while self.running:
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
                    self._last_tick = time.monotonic()
                    continue
                self._drain_async_events()
                self._schedule_git_control_refresh()
                if self.mode == MODE_EXPLORER and not self._file_tree_feature.entries:
                    self._schedule_file_tree_refresh()
                self._write_swap_if_needed()
                self.render()
                time.sleep(0.01)
        finally:
            self._save_session()
            self._write_swap_if_needed(force=True)
            self._async_runtime.close()
            self._console.exit()


# Backward-compat alias for older imports.
PviEditor = PvimEditor
