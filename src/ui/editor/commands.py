from __future__ import annotations

import shlex

from ...core.config import AppConfig
from ...features.modules.git_control import GitSnapshot
from ..layout import NotificationCenter
from .modes import MODE_EXPLORER


class CommandsMixin:
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

        if cmd in {"undo", "u"}:
            self._undo()
            return

        if cmd in {"redo"}:
            self._redo()
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
                "Commands: :w :q :e :find :replace :rename :format :fuzzy :grep :tree :feature :workspace :session :swap :keys :script :plugin :proc :virtual :ast :profile :piece :termcaps :lsp :diag"
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

        if cmd in {"grep", "livegrep", "live_grep"}:
            self._open_live_grep(" ".join(args))
            return

        if cmd in {"workspace", "project"}:
            self._set_message(f"Workspace: {self._workspace_root}")
            return

        if cmd in {"diag", "diagnostic", "diagnostics"}:
            self._show_lsp_diagnostics()
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

        if cmd == "lsp":
            action = args[0] if args else "status"
            if action == "status":
                if not self._lsp_enabled:
                    self._set_message("LSP: disabled")
                    return
                if not self._lsp_command:
                    self._set_message("LSP: command not configured", error=True)
                    return
                running = self._lsp_client.running if self._lsp_client is not None else False
                state = "running" if running else "idle"
                self._set_message(f"LSP: {state} ({' '.join(self._lsp_command)})")
                return
            if action == "start":
                client = self._ensure_lsp_ready(show_error=True)
                if client is not None:
                    self._set_message("LSP started.")
                return
            if action == "stop":
                if self._lsp_client is None:
                    self._set_message("LSP: not running.")
                    return
                try:
                    self._async_runtime.run_sync(self._lsp_client.stop(), timeout=self._lsp_timeout_seconds)
                    self._set_message("LSP stopped.")
                except Exception as exc:
                    self._set_message(f"LSP stop failed: {exc}", error=True)
                return
            self._set_message("Usage: :lsp status|start|stop  (:diag to show diagnostics)", error=True)
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

        if cmd == "session":
            action = args[0] if args else "save"
            if action == "save":
                self._save_session()
                self._set_message(f"Session saved: {self._session_path}")
                return
            if action == "load":
                self._restore_session()
                self._set_message("Session restored.")
                return
            self._set_message("Usage: :session save|load", error=True)
            return

        if cmd == "swap":
            action = args[0] if args else "write"
            if action == "write":
                self._write_swap_if_needed(force=True)
                if self.file_path is not None:
                    self._set_message(f"Swap written: {self._persistence.swap_path(self.file_path)}")
                else:
                    self._set_message("No file for swap.", error=True)
                return
            if action == "clear":
                if self.file_path is not None:
                    self._persistence.remove_swap(self.file_path)
                    self._set_message("Swap cleared.")
                else:
                    self._set_message("No file for swap.", error=True)
                return
            self._set_message("Usage: :swap write|clear", error=True)
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
