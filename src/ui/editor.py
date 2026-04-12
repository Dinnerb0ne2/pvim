from __future__ import annotations

import asyncio
import cProfile
import io
import json
import os
from pathlib import Path
import pstats
import shlex
import sys
import time

from .. import APP_NAME, APP_VERSION
from ..core.async_runtime import AsyncRuntime
from ..core.buffer import Buffer
from ..core.config import AppConfig
from ..core.console import CSI, ConsoleController, KeyReader
from ..core.process_pipe import AsyncProcessManager
from ..core.terminal_capabilities import detect_terminal_capabilities
from ..core.theme import RESET, Theme, load_theme
from ..features.file_index import FileIndex
from ..features.formatter import normalize_code_style, organize_python_imports
from ..features.fuzzy import fuzzy_filter
from ..features.git_status import GitStatusProvider
from ..features.ast_query import AstQueryService
from ..features.modules import FileTreeFeature, GitControlFeature, TabCompletionFeature
from ..features.modules.git_control import GitSnapshot
from ..features.refactor import find_next, rename_symbol, replace_all, replace_next, word_at_cursor
from ..features.syntax import PLAIN_PROFILE, SyntaxManager
from ..plugins import PluginManager
from ..scripting import ScriptError
from .floating_list import FloatingList
from .layout import FeatureDescriptor, FeatureRegistry, LayoutContext, LayoutManager, NotificationCenter

MODE_NORMAL = "NORMAL"
MODE_INSERT = "INSERT"
MODE_COMMAND = "COMMAND"
MODE_VISUAL = "VISUAL"
MODE_FUZZY = "FUZZY"
MODE_FLOAT_LIST = "FLOAT_LIST"
MODE_EXPLORER = "EXPLORER"
MODE_COMPLETION = "COMPLETION"
MODE_KEY_HINTS = "KEY_HINTS"
MODE_ALERT = "ALERT"


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


class PvimEditor:
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
        self.show_sidebar = self.config.sidebar_enabled()
        self.sidebar_width = self.config.sidebar_width()
        self.key_hints_enabled = self.config.key_hints_enabled()
        self.key_hints_trigger = self.config.key_hints_trigger()
        self._lazy_load_enabled = self.config.lazy_load_enabled()
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
            "",
            "Tools",
            "  Ctrl+F quick find",
            "  Ctrl+G quick replace",
            "  Ctrl+N tab completion",
            "  Ctrl+P fuzzy finder",
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

        if self.cx < self.col_offset:
            self.col_offset = self.cx
        elif self.cx >= self.col_offset + text_cols:
            self.col_offset = self.cx - text_cols + 1

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
        while index > 0 and _is_word_char(line[index - 1]):
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
        while index < length and _is_word_char(line[index]):
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
        while start > 0 and _is_word_char(line[start - 1]):
            start -= 1
        end = self.cx
        while end < len(line) and _is_word_char(line[end]):
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
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self._set_message(f"Open failed: {exc}", error=True)
                return False
            self._set_message(f"Opened {path}")
        else:
            self._set_message(f"New file: {path}")

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
        self.visual_anchor = None
        self._clear_multi_cursor()
        self._syntax_profile = self._syntax_manager().profile_for_file(self.file_path)
        if self.file_path is not None:
            tab_label = self.file_path.name
            if tab_label not in self._tab_items:
                self._tab_items.append(tab_label)
            self._current_tab_index = self._tab_items.index(tab_label)
        self._schedule_git_control_refresh(force=True)
        return True

    def save_file(self, target: Path | str | None = None) -> bool:
        if target is not None:
            self.file_path = self._resolve_path(target)

        if self.file_path is None:
            self._set_message("No file name. Use :w <path>.", error=True)
            return False

        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(self.buffer.text(), encoding="utf-8")
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
        self._set_message(f"Wrote {self.file_path}")
        return True

    def request_quit(self, *, force: bool) -> bool:
        if self.modified and not force:
            self._set_message("No write since last change. Use :q! to discard.", error=True)
            return False
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
        longest_item = max((len(item) for item in popup.items), default=20)
        content_width = max(len(title) + 2, len(footer) + 2, longest_item + 2)
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
            heading = heading[:content_width]
            top_line = top_left + heading.center(content_width, horizontal) + top_right
            return f"{border_style}{(' ' * left) + top_line + (' ' * right_pad)}{RESET}"

        if row == popup_height - 1:
            bottom_line = bottom_left + (horizontal * content_width) + bottom_right
            return f"{border_style}{(' ' * left) + bottom_line + (' ' * right_pad)}{RESET}"

        if row == popup_height - 2 and footer:
            footer_line = vertical + footer[:content_width].ljust(content_width) + vertical
            return f"{border_style}{(' ' * left) + footer_line + (' ' * right_pad)}{RESET}"

        index = popup.scroll + row - 1
        if 0 <= index < len(popup.items):
            text = popup.items[index][:content_width].ljust(content_width)
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
            return f"{selected_style}{head[:text_cols].ljust(text_cols)}{RESET}"

        content_row = self.key_hint_scroll + screen_row - 1
        if 0 <= content_row < len(self.key_hint_lines):
            text = self.key_hint_lines[content_row]
            return f"{base_style}{text[:text_cols].ljust(text_cols)}{RESET}"

        return f"{base_style}{' ' * text_cols}{RESET}"

    def _render_alert_row(self, screen_row: int, text_cols: int) -> str:
        base_style = self.theme.ui_style("editor")
        error_style = self.theme.ui_style("message_error")

        if screen_row == 0:
            head = "Script Error (Esc/Enter close)"
            return f"{error_style}{head[:text_cols].ljust(text_cols)}{RESET}"

        content_row = screen_row - 1
        if 0 <= content_row < len(self.alert_lines):
            text = self.alert_lines[content_row]
            return f"{base_style}{text[:text_cols].ljust(text_cols)}{RESET}"
        return f"{base_style}{' ' * text_cols}{RESET}"

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

        line_index = self.row_offset + screen_row
        if line_index < len(self.lines):
            raw = self.lines[line_index]
            visible_code = raw[self.col_offset : self.col_offset + text_cols]
            ghost_chunks = self.buffer.get_virtual_text(line_index)
            ghost_visible = ""
            if ghost_chunks:
                ghost_raw = "  <<" + " | ".join(ghost_chunks) + ">>"
                room = max(0, text_cols - len(visible_code))
                ghost_visible = ghost_raw[:room]
            padding = " " * max(0, text_cols - len(visible_code) - len(ghost_visible))

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
            message = hints[screen_row - start][:text_cols]
            line = message.center(text_cols)
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
            return f"{header_style}{title[:width].ljust(width)}{RESET}"

        file_index = start_index + screen_row - 1
        if file_index < len(files):
            relative = files[file_index]
            marker = self.git.status_for_relative(relative) if self.config.feature_enabled("git_status") else " "
            marker = marker if marker.strip() else " "
            body = f"{marker} {relative}"
            text = body[:width].ljust(width)
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
            col=self.cx + 1,
            branch=git_label,
        )
        text = self._layout_manager.render_statusline(context)
        return f"{self.theme.ui_style('status')}{text}{RESET}"

    def _render_bottom_row(self, width: int) -> tuple[str, int]:
        if self.mode == MODE_COMMAND:
            text = ":" + self.command_text
            if len(text) <= width:
                visible = text.ljust(width)
                cursor_col = len(text) + 1
            else:
                visible = text[-width:]
                cursor_col = width
            return f"{self.theme.ui_style('command_line')}{visible}{RESET}", clamp(cursor_col, 1, width)

        if self.mode == MODE_FUZZY:
            text = f"fuzzy> {self.fuzzy_query}"
            if len(text) <= width:
                visible = text.ljust(width)
                cursor_col = len(text) + 1
            else:
                visible = text[-width:]
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

        line = self.message[:width].ljust(width)
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
            col=self.cx + 1,
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
            cursor_row = plan.editor_top + clamp(self.cy - self.row_offset + 1, 1, text_rows)
            cursor_col = clamp(sidebar_width + gutter + (self.cx - self.col_offset) + 1, 1, width)

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
        content_width = max(len(title), *(len(item) for item in visible))
        box_width = clamp(content_width + 2, 20, max(20, width))
        if box_width >= width:
            box_width = width
        left = max(0, width - box_width)

        top = top_left + title.center(max(0, box_width - 2), horizontal) + top_right
        bottom = bottom_left + (horizontal * max(0, box_width - 2)) + bottom_right
        rows = [top]
        for item in visible:
            rows.append(vertical + item[: max(0, box_width - 2)].ljust(max(0, box_width - 2)) + vertical)
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

    def execute_command(self, command: str) -> None:
        command = command.strip()
        if not command:
            return

        try:
            parts = shlex.split(command)
        except ValueError as exc:
            self._set_message(f"Command parse failed: {exc}", error=True)
            return

        if not parts:
            return
        cmd = parts[0]
        args = parts[1:]

        if cmd in {"w", "write"}:
            if args:
                self.save_file(args[0])
            else:
                self.save_file()
            return

        if cmd in {"q", "quit"}:
            self.request_quit(force=False)
            return

        if cmd in {"q!", "quit!"}:
            self.request_quit(force=True)
            return

        if cmd in {"wq", "x"}:
            if self.save_file():
                self.request_quit(force=True)
            return

        if cmd in {"e", "edit"} and args:
            self.open_file(args[0], force=False)
            return

        if cmd in {"e!", "edit!"} and args:
            self.open_file(args[0], force=True)
            return

        if cmd == "set" and args:
            option = args[0]
            if option in {"number", "nu"}:
                self.show_line_numbers = True
                self._set_message("Line numbers: on")
                return
            if option in {"nonumber", "nonu"}:
                self.show_line_numbers = False
                self._set_message("Line numbers: off")
                return
            if option in {"sidebar", "side"}:
                self.show_sidebar = True
                self._set_message("Sidebar: on")
                return
            if option in {"nosidebar", "noside"}:
                self.show_sidebar = False
                self._set_message("Sidebar: off")
                return
            self._set_message(f"Unknown set option: {option}", error=True)
            return

        if cmd in {"help", "h"}:
            self._set_message(
                "Commands: :w :q :e :find :replace :rename :format :fuzzy :tree :feature :keys :script :plugin :proc :virtual :ast :profile :piece :termcaps"
            )
            return

        if cmd in {"keys", "keyhint", "keymap"}:
            self._open_key_hints()
            return

        if cmd in {"find", "search"}:
            query = " ".join(args)
            self._find(query)
            return

        if cmd == "replace":
            if len(args) < 2:
                self._set_message("Usage: :replace <old> <new>", error=True)
                return
            self._replace_next(args[0], args[1])
            return

        if cmd in {"replaceall", "replace_all"}:
            if len(args) < 2:
                self._set_message("Usage: :replaceall <old> <new>", error=True)
                return
            self._replace_all(args[0], args[1])
            return

        if cmd == "rename":
            if len(args) < 2:
                self._set_message("Usage: :rename <old> <new>", error=True)
                return
            self._rename_symbol(args[0], args[1])
            return

        if cmd == "refactor":
            if not args:
                self._set_message("Usage: :refactor rename/imports ...", error=True)
                return
            action = args[0]
            if action == "rename" and len(args) >= 3:
                self._rename_symbol(args[1], args[2])
                return
            if action in {"imports", "organize-imports"}:
                self._refactor_imports()
                return
            self._set_message(f"Unknown refactor action: {action}", error=True)
            return

        if cmd == "format":
            self._format_code()
            return

        if cmd == "fuzzy":
            self._open_fuzzy(" ".join(args))
            return

        if cmd in {"files-refresh", "refresh-files"}:
            self.file_index.refresh(force=True)
            self._set_message("File index refreshed.")
            return

        if cmd == "script":
            if not args:
                self._set_message("Usage: :script run <path>", error=True)
                return
            action = args[0]
            if action == "run" and len(args) >= 2:
                self._run_script_file(args[1])
                return
            self._set_message("Usage: :script run <path>", error=True)
            return

        if cmd == "profile":
            if len(args) >= 2 and args[0] == "script":
                self._profile_script_file(args[1])
                return
            self._set_message("Usage: :profile script <path>", error=True)
            return

        if cmd == "plugin":
            self._handle_plugin_command(args)
            return

        if cmd == "proc":
            if not args:
                self._set_message("Usage: :proc start|read|write|stop|status ...", error=True)
                return
            action = args[0]
            if action == "start" and len(args) >= 2:
                command_text = " ".join(args[1:])
                process_id = self._process_manager.start(command_text)
                self._set_message(f"Process started: {process_id}")
                return
            if action == "read" and len(args) >= 2:
                process_id = int(args[1])
                max_lines = int(args[2]) if len(args) >= 3 else 20
                lines = self._process_manager.read(process_id, max_lines=max_lines)
                if lines:
                    self._show_alert("\n".join(lines))
                else:
                    self._set_message("No process output.")
                return
            if action == "write" and len(args) >= 3:
                process_id = int(args[1])
                text = " ".join(args[2:])
                ok = self._process_manager.write(process_id, text)
                self._set_message("Process write sent." if ok else "Process write failed.")
                return
            if action == "stop" and len(args) >= 2:
                process_id = int(args[1])
                ok = self._process_manager.stop(process_id)
                self._set_message("Process stop sent." if ok else "Process stop failed.")
                return
            if action == "status" and len(args) >= 2:
                process_id = int(args[1])
                self._set_message(f"Process {process_id}: {self._process_manager.status(process_id)}")
                return
            self._set_message("Usage: :proc start|read|write|stop|status ...", error=True)
            return

        if cmd == "virtual":
            if not args:
                self._set_message("Usage: :virtual add|set|clear|get ...", error=True)
                return
            action = args[0]
            if action in {"add", "set"} and len(args) >= 3:
                row = int(args[1]) - 1
                if row < 0 or row >= len(self.lines):
                    self._set_message("Invalid row for virtual text.", error=True)
                    return
                text = " ".join(args[2:])
                if action == "add":
                    self.buffer.add_virtual_text(row, text)
                else:
                    self.buffer.set_virtual_text(row, [text] if text else [])
                self._set_message("Virtual text updated.")
                return
            if action == "clear":
                if len(args) >= 2:
                    self.buffer.clear_virtual_text(int(args[1]) - 1)
                else:
                    self.buffer.clear_virtual_text()
                self._set_message("Virtual text cleared.")
                return
            if action == "get" and len(args) >= 2:
                row = int(args[1]) - 1
                text = " | ".join(self.buffer.get_virtual_text(row))
                self._set_message(text or "(no virtual text)")
                return
            self._set_message("Usage: :virtual add|set|clear|get ...", error=True)
            return

        if cmd == "ast":
            row = int(args[0]) if len(args) >= 1 else self.cy + 1
            col = int(args[1]) if len(args) >= 2 else self.cx + 1
            kinds = args[2] if len(args) >= 3 else "function,class"
            result = self._plugin_api_dispatch(1, "ast.node_at", [row, col, kinds])
            if not isinstance(result, str) or not result:
                self._set_message("AST node not found.", error=True)
                return
            self._set_message(f"AST {result}")
            return

        if cmd == "piece":
            stats = self.buffer.piece_table_stats()
            self._set_message(
                f"PieceTable enabled={stats['enabled']} lines={stats['line_count']} len={stats['length']} dirty={stats['dirty']}"
            )
            return

        if cmd == "termcaps":
            caps = self._terminal_capabilities
            self._set_message(
                f"Terminal truecolor={caps.true_color} colors={caps.color_level} unicode={caps.unicode_ui}"
            )
            return

        if cmd in {"sidebar"}:
            if not args:
                self._set_message("Usage: :sidebar on|off|toggle", error=True)
                return
            option = args[0]
            if option == "on":
                self.show_sidebar = True
                self._set_message("Sidebar: on")
                return
            if option == "off":
                self.show_sidebar = False
                self._set_message("Sidebar: off")
                return
            if option == "toggle":
                self._toggle_sidebar()
                return
            self._set_message("Usage: :sidebar on|off|toggle", error=True)
            return

        if cmd in {"tree", "explorer"}:
            action = args[0] if args else "open"
            if action in {"open", "show"}:
                self._open_explorer(refresh=False)
                return
            if action in {"refresh", "reload"}:
                self._open_explorer(refresh=True)
                return
            if action in {"close", "hide"}:
                self._close_explorer()
                return
            if action == "toggle":
                if self.mode == MODE_EXPLORER:
                    self._close_explorer()
                else:
                    self._open_explorer(refresh=False)
                return
            self._set_message("Usage: :tree open|refresh|close|toggle", error=True)
            return

        if cmd == "feature":
            if len(args) < 2:
                self._set_message("Usage: :feature <name> <on|off>", error=True)
                return
            name = args[0]
            state = args[1].lower()
            enabled = state == "on"
            if state not in {"on", "off"}:
                self._set_message("Usage: :feature <name> <on|off>", error=True)
                return
            if not self._feature_registry.set_enabled(name, enabled):
                self._set_message(f"Unknown feature: {name}", error=True)
                return
            if name == "file_tree":
                self._file_tree_feature.enabled = enabled
                if enabled:
                    self._schedule_file_tree_refresh()
                else:
                    self._close_explorer()
            elif name == "tab_completion":
                self._completion_feature.enabled = enabled
                if not enabled:
                    self._close_tab_completion()
            elif name == "git_control":
                self._git_control_feature.enabled = enabled
                if enabled:
                    self._schedule_git_control_refresh(force=True)
                else:
                    self._git_control_feature.apply(GitSnapshot(branch="-", file_state="clean", line_markers={}))
            elif name == "notifications" and not enabled:
                self._notifications = NotificationCenter()
            self._set_message(f"Feature {name}: {'on' if enabled else 'off'}")
            return

        if cmd in {"reload-config", "config-reload"}:
            self.config = AppConfig.load(self.config.path)
            self._apply_runtime_config()
            self._set_message("Configuration reloaded.")
            return

        self._set_message(f"Unknown command: {command}", error=True)

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

    def _insert_printable(self, key: str) -> None:
        if not self._auto_pairs:
            self._insert_text_multi(key)
            return

        line = self._line()
        if key in self._auto_pairs:
            closing = self._auto_pairs[key]
            if key == closing:
                if self.cx < len(line) and line[self.cx] == key:
                    self.cx += 1
                    return
                self._insert_text_multi(key + closing)
                self.cx -= 1
                return

            self._insert_text_multi(key + closing)
            self.cx -= 1
            return

        if key in self._auto_pair_closers and self.cx < len(line) and line[self.cx] == key:
            self.cx += 1
            return

        self._insert_text_multi(key)

    def _handle_command_key(self, key: str) -> None:
        if key == "ESC":
            self.mode = MODE_NORMAL
            self.command_text = ""
            self._set_message("Command cancelled.")
            return

        if key == "BACKSPACE":
            self.command_text = self.command_text[:-1]
            return

        if key == "ENTER":
            command = self.command_text
            self.command_text = ""
            self.mode = MODE_NORMAL
            self.execute_command(command)
            return

        if len(key) == 1 and key.isprintable():
            self.command_text += key

    def _handle_fuzzy_key(self, key: str) -> None:
        if key == "ESC":
            self.mode = MODE_NORMAL
            self.fuzzy_query = ""
            self.fuzzy_matches = []
            self.fuzzy_index = 0
            self._floating_list = None
            self._floating_source = ""
            self._set_message("Fuzzy cancelled.")
            return

        if key == "ENTER":
            self._accept_fuzzy_selection()
            return

        if key == "BACKSPACE":
            self.fuzzy_query = self.fuzzy_query[:-1]
            self._update_fuzzy_matches()
            return

        if key in {"UP", "CTRL_LEFT"}:
            if self._floating_list is not None:
                self._floating_list.move_up()
                self.fuzzy_index = self._floating_list.selected
            else:
                self.fuzzy_index = max(0, self.fuzzy_index - 1)
            return

        if key in {"DOWN", "CTRL_RIGHT"}:
            if self._floating_list is not None:
                _, height = self._terminal_size()
                self._floating_list.move_down(max(1, height - 3))
                self.fuzzy_index = self._floating_list.selected
            else:
                self.fuzzy_index = min(max(0, len(self.fuzzy_matches) - 1), self.fuzzy_index + 1)
            return

        if len(key) == 1 and key.isprintable():
            self.fuzzy_query += key
            self._update_fuzzy_matches()

    def _handle_key_hints_key(self, key: str) -> None:
        if key in {"ESC", "ENTER", self.key_hints_trigger}:
            self.mode = MODE_NORMAL
            return
        if key == "UP":
            self.key_hint_scroll = max(0, self.key_hint_scroll - 1)
            return
        if key == "DOWN":
            max_scroll = max(0, len(self.key_hint_lines) - 1)
            self.key_hint_scroll = min(max_scroll, self.key_hint_scroll + 1)

    def _handle_alert_key(self, key: str) -> None:
        if key in {"ESC", "ENTER"}:
            self._close_alert()

    def _handle_floating_list_key(self, key: str) -> None:
        popup = self._floating_list
        if popup is None:
            self.mode = MODE_NORMAL
            return

        if key == "ESC":
            self._floating_list = None
            self._floating_source = ""
            self.mode = MODE_NORMAL
            return

        if key in {"UP", "CTRL_LEFT"}:
            popup.move_up()
            return

        if key in {"DOWN", "CTRL_RIGHT"}:
            _, height = self._terminal_size()
            popup.move_down(max(1, height - 3))
            return

        if key == "ENTER":
            selected = popup.selected_item()
            if selected is not None:
                self._set_message(selected)
            self._floating_list = None
            self._floating_source = ""
            self.mode = self._floating_accept_mode

    def _handle_explorer_key(self, key: str) -> None:
        if key == "ESC":
            self._close_explorer()
            return
        visible_rows = max(1, self._terminal_size()[1] - 8)
        if key in {"UP", "CTRL_LEFT"}:
            self._file_tree_feature.move_up()
            self._sync_file_tree_popup()
            return
        if key in {"DOWN", "CTRL_RIGHT"}:
            self._file_tree_feature.move_down(visible_rows)
            self._sync_file_tree_popup()
            return
        if key == "ENTER":
            selected = self._file_tree_feature.selected_path()
            if not selected:
                return
            if self.open_file(Path.cwd() / selected, force=False):
                self._close_explorer()

    def _handle_completion_key(self, key: str) -> None:
        visible_rows = max(1, self._terminal_size()[1] - 8)
        if key == "ESC":
            self._close_tab_completion()
            return
        if key in {"UP", "CTRL_LEFT"}:
            self._completion_feature.move_up()
            self._sync_completion_popup()
            return
        if key in {"DOWN", "CTRL_RIGHT"}:
            self._completion_feature.move_down(visible_rows)
            self._sync_completion_popup()
            return
        if key in {"TAB", "ENTER"}:
            self._accept_tab_completion()
            return
        if key == "BACKSPACE":
            self._close_tab_completion()
            self._backspace()
            return
        if len(key) == 1 and key.isprintable():
            self._close_tab_completion()
            self._insert_printable(key)
            return
        if key == "LEFT":
            self._close_tab_completion()
            self._move_left()
            return
        if key == "RIGHT":
            self._close_tab_completion()
            self._move_right()
            return

    def _handle_visual_key(self, key: str) -> None:
        if key in {"ESC"}:
            self.mode = MODE_NORMAL
            self.visual_anchor = None
            self._set_message("-- NORMAL --")
            return

        if key in {"k", "UP"}:
            self._move_up()
            return
        if key in {"j", "DOWN"}:
            self._move_down()
            return
        if key in {"h", "LEFT"}:
            self._move_left()
            return
        if key in {"l", "RIGHT"}:
            self._move_right()
            return

        if key in {"TAB", ">"}:
            selected = self._selected_line_range()
            if selected is not None:
                self._indent_lines(selected[0], selected[1])
            return

        if key in {"SHIFT_TAB", "<"}:
            selected = self._selected_line_range()
            if selected is not None:
                self._outdent_lines(selected[0], selected[1])
            return

        if key == "i":
            selected = self._selected_line_range()
            if selected is None:
                return
            start, end = selected
            self.extra_cursor_lines = [line for line in range(start, end + 1) if line != self.cy]
            self.mode = MODE_INSERT
            self.visual_anchor = None
            self._set_message(f"-- INSERT -- multi-cursor {1 + len(self.extra_cursor_lines)}")
            return

        if key == ":":
            self._enter_command()

    def _handle_insert_key(self, key: str) -> None:
        if key == "ESC":
            self.mode = MODE_NORMAL
            self.visual_anchor = None
            self._set_message("-- NORMAL --")
            return

        if key == "BACKSPACE":
            self._backspace()
            return

        if key == "ENTER":
            self._insert_newline()
            return

        if key == "TAB":
            if self._open_tab_completion():
                return
            self._insert_text_multi(" " * self.tab_size)
            return

        if key == "DEL":
            self._delete_char()
            return

        if key == "LEFT":
            self._move_left()
            return

        if key == "RIGHT":
            self._move_right()
            return

        if key == "UP":
            self._move_up()
            return

        if key == "DOWN":
            self._move_down()
            return

        if key == "HOME":
            self.cx = 0
            return

        if key == "END":
            self.cx = len(self._line())
            return

        if key == "PGUP":
            self._page_up()
            return

        if key == "PGDN":
            self._page_down()
            return

        if len(key) == 1 and key.isprintable():
            self._insert_printable(key)

    def _handle_normal_key(self, key: str) -> None:
        if key in {"h", "LEFT"}:
            self._move_left()
            self.pending_operator = ""
            return

        if key in {"l", "RIGHT"}:
            self._move_right()
            self.pending_operator = ""
            return

        if key in {"k", "UP"}:
            self._move_up()
            self.pending_operator = ""
            return

        if key in {"j", "DOWN"}:
            self._move_down()
            self.pending_operator = ""
            return

        if key in {"HOME", "0"}:
            self.cx = 0
            self.pending_operator = ""
            return

        if key in {"END", "$"}:
            self.cx = len(self._line())
            self.pending_operator = ""
            return

        if key == "PGUP":
            self._page_up()
            self.pending_operator = ""
            return

        if key == "PGDN":
            self._page_down()
            self.pending_operator = ""
            return

        if key in {"i"}:
            self.mode = MODE_INSERT
            self.pending_operator = ""
            self._set_message("-- INSERT --")
            return

        if key in {"a"}:
            self.cx = min(self.cx + 1, len(self._line()))
            self.mode = MODE_INSERT
            self.pending_operator = ""
            self._set_message("-- INSERT --")
            return

        if key in {"A"}:
            self.cx = len(self._line())
            self.mode = MODE_INSERT
            self.pending_operator = ""
            self._set_message("-- INSERT --")
            return

        if key in {"o"}:
            self._open_line_below()
            self.pending_operator = ""
            return

        if key in {"x", "DEL"}:
            self._delete_char()
            self.pending_operator = ""
            return

        if key == "d":
            if self.pending_operator == "d":
                self._delete_line()
                self.pending_operator = ""
            else:
                self.pending_operator = "d"
                self._set_message("d")
            return

        if key == ":":
            self._enter_command()
            return

        if key == "/":
            self._enter_command("find ")
            return

        if key == "V":
            self.mode = MODE_VISUAL
            self.visual_anchor = self.cy
            self.pending_operator = ""
            self._set_message("-- VISUAL LINE --")
            return

        if key == ">":
            self._indent_lines(self.cy, self.cy)
            self.pending_operator = ""
            return

        if key == "<":
            self._outdent_lines(self.cy, self.cy)
            self.pending_operator = ""
            return

        if key == "F2":
            self.show_line_numbers = not self.show_line_numbers
            state = "on" if self.show_line_numbers else "off"
            self._set_message(f"Line numbers: {state}")
            self.pending_operator = ""
            return

        if key == "F4":
            self._toggle_sidebar()
            self.pending_operator = ""
            return

        if key == "ESC":
            self.pending_operator = ""
            self._clear_multi_cursor()

    def _read_key(self) -> str:
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

    def run(self) -> None:
        self._console.enter()
        try:
            while self.running:
                if KeyReader.has_key():
                    key = self._read_key()
                    self.handle_key(key)
                    self.render()
                    self._drain_async_events(max_items=8)
                    self._schedule_git_control_refresh()
                    if self.mode == MODE_EXPLORER and not self._file_tree_feature.entries:
                        self._schedule_file_tree_refresh()
                    self._last_tick = time.monotonic()
                    continue
                self._drain_async_events()
                self._schedule_git_control_refresh()
                if self.mode == MODE_EXPLORER and not self._file_tree_feature.entries:
                    self._schedule_file_tree_refresh()
                self.render()
                time.sleep(0.01)
        finally:
            self._async_runtime.close()
            self._console.exit()


# Backward-compat alias for older imports.
PviEditor = PvimEditor
