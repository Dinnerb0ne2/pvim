from __future__ import annotations

import shlex

from ...core.config import AppConfig
from ...features.git_tools import (
    blame_line,
    checkout_branch,
    current_file_diff,
    list_branches,
    stage_file,
    status_short,
    unstage_file,
)
from ...features.modules.git_control import GitSnapshot
from ..layout import NotificationCenter
from .modes import MODE_EXPLORER, MODE_TERMINAL


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
            self._handle_undo_command(args)
            return

        if cmd in {"redo"}:
            self._handle_undo_command(["redo", *args])
            return

        if cmd in {"e", "edit"} and args:
            self.open_file(args[0], force=False)
            return

        if cmd in {"e!", "edit!"} and args:
            self.open_file(args[0], force=True)
            return

        if cmd in {"split", "sp"}:
            self._open_split("horizontal")
            return

        if cmd in {"vsplit", "vsp"}:
            self._open_split("vertical")
            return

        if cmd in {"only"}:
            self._close_split()
            return

        if cmd == "wincmd" and args:
            action = args[0].lower()
            if action == "w":
                self._toggle_split_focus()
                return
            if action == "h":
                self._resize_split(-0.05)
                return
            if action == "l":
                self._resize_split(0.05)
                return
            if action in {"q", "c"}:
                self._close_split()
                return
            self._set_message("Usage: :wincmd w|h|l|q", error=True)
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
                self._sidebar_manual_override = True
                self.show_sidebar = True
                self._set_message("Sidebar: on")
                return
            if option in {"nosidebar", "noside"}:
                self._sidebar_manual_override = True
                self.show_sidebar = False
                self._set_message("Sidebar: off")
                return
            if option == "encoding":
                if len(args) < 2:
                    self._set_message("Usage: :set encoding <name>", error=True)
                    return
                self._set_encoding(args[1])
                return
            if option.startswith("encoding="):
                self._set_encoding(option.split("=", 1)[1])
                return
            self._set_message(f"Unknown set option: {option}", error=True)
            return

        if cmd in {"help", "h"}:
            topic = " ".join(args)
            self._show_alert(self._help_text(topic))
            return

        if cmd in {"keys", "keyhint", "keymap"}:
            if not args and cmd == "keyhint":
                self._open_key_hints()
            else:
                self._handle_keys_command(args)
            return

        if cmd in {"quickfix", "qf"}:
            self._handle_quickfix_command(args)
            return

        if cmd in {"fold", "zf"}:
            self._handle_fold_command(args)
            return

        if cmd == "autocmd":
            self._handle_autocmd_command(args)
            return

        if cmd in {"var", "vars"}:
            self._handle_var_command(args)
            return

        if cmd in {"macro", "macros"}:
            self._handle_macro_command(args)
            return

        if cmd in {"clip", "clipboard"}:
            self._handle_clipboard_command(args)
            return

        if cmd in {"stdlib", "std"}:
            self._handle_stdlib_command(args)
            return

        if cmd in {"iselect", "isel"}:
            self._handle_incremental_select_command(args)
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

        if cmd in {"findre", "searchre"}:
            if not args:
                self._set_message("Usage: :findre <pattern> [flags]", error=True)
                return
            flags = args[1] if len(args) >= 2 else ""
            self._find_regex(args[0], flags)
            return

        if cmd in {"replacere", "replace_re"}:
            if len(args) < 2:
                self._set_message("Usage: :replacere <pattern> <replacement> [flags]", error=True)
                return
            flags = args[2] if len(args) >= 3 else ""
            self._replace_regex_next(args[0], args[1], flags)
            return

        if cmd in {"replaceallre", "replace_all_re"}:
            if len(args) < 2:
                self._set_message("Usage: :replaceallre <pattern> <replacement> [flags]", error=True)
                return
            flags = args[2] if len(args) >= 3 else ""
            self._replace_regex_all(args[0], args[1], flags)
            return

        if cmd in {"replaceproj", "replaceproject"}:
            if len(args) < 2:
                self._set_message("Usage: :replaceproj <old> <new>", error=True)
                return
            self._replace_all_project(args[0], args[1])
            return

        if cmd in {"replaceprojre", "replaceprojectre"}:
            if len(args) < 2:
                self._set_message("Usage: :replaceprojre <pattern> <replacement> [flags]", error=True)
                return
            flags = args[2] if len(args) >= 3 else ""
            self._replace_regex_all_project(args[0], args[1], flags)
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

        if cmd == "workspace":
            self._set_message(f"Workspace: {self._workspace_root}")
            return

        if cmd == "runtime":
            lines = [
                "Runtime Manager",
                "",
                f"root: {self._runtime_root}",
                f"session: {self._session_path}",
                f"profiles: {self._session_profiles_dir}",
                f"swap_dir: {self.config.swap_directory()}",
                f"plugins: {self.config.plugins_directory()}",
                f"themes: {(self._runtime_root / 'themes').resolve()}",
                f"keymaps: {self._shortcut_overrides_file()}",
            ]
            self._show_alert("\n".join(lines))
            return

        if cmd in {"jump", "jumps"}:
            action = args[0].lower() if args else "back"
            if action in {"back", "prev", "previous"}:
                self._jump_back()
                return
            if action in {"forward", "next"}:
                self._jump_forward()
                return
            if action in {"list", "ls"}:
                self._show_alert("\n".join(self._jump_list_lines()))
                return
            self._set_message("Usage: :jump back|forward|list", error=True)
            return

        if cmd in {"project", "project!"}:
            force = cmd.endswith("!")
            if not args:
                self._set_message(f"Workspace: {self._workspace_root}")
                return
            if args[0] in {"open", "o"}:
                if len(args) < 2:
                    self._set_message("Usage: :project open <directory>", error=True)
                    return
                self.open_project(args[1], force=force)
                return
            self.open_project(args[0], force=force)
            return

        if cmd in {"diag", "diagnostic", "diagnostics"}:
            self._show_lsp_diagnostics()
            return

        if cmd in {"codeaction", "codeactions", "ca"}:
            self._open_code_actions()
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
            action = args[0].lower() if args else "status"
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
            if action in {"refs", "references"}:
                self._show_lsp_references()
                return
            if action in {"impl", "implementation"}:
                self._show_lsp_implementation()
                return
            if action in {"symbols", "outline"}:
                self._show_lsp_document_symbols(" ".join(args[1:]))
                return
            if action in {"wsymbol", "workspace-symbol", "workspace-symbols"}:
                self._show_lsp_workspace_symbols(" ".join(args[1:]))
                return
            if action == "rename":
                if len(args) < 2:
                    self._set_message("Usage: :lsp rename <new_name>", error=True)
                    return
                self._lsp_rename_symbol(" ".join(args[1:]))
                return
            if action == "format":
                self._lsp_format_document()
                return
            self._set_message(
                "Usage: :lsp status|start|stop|refs|impl|symbols|wsymbol <q>|rename <name>|format",
                error=True,
            )
            return

        if cmd in {"dap", "debug"}:
            self._handle_dap_command(args)
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

        if cmd in {"term", "terminal"}:
            if not args:
                self._open_terminal(None)
                return
            action = args[0].strip().lower()
            if action in {"open", "start", "run"}:
                command_text = " ".join(args[1:]).strip()
                self._open_terminal(command_text or None)
                return
            if action in {"new", "spawn"}:
                command_text = " ".join(args[1:]).strip()
                self._open_terminal(command_text or None, force_new=True)
                return
            if action in {"split", "vsp", "hsplit"}:
                if len(args) >= 2:
                    token = args[1].strip().lower()
                    if token in {"on", "1", "true"}:
                        self._terminal_split_view = True
                    elif token in {"off", "0", "false"}:
                        self._terminal_split_view = False
                    elif token in {"toggle", "tog"}:
                        self._terminal_split_view = not self._terminal_split_view
                    else:
                        self._set_message("Usage: :term split [on|off|toggle]", error=True)
                        return
                else:
                    self._terminal_split_view = not self._terminal_split_view
                if self._terminal_split_view and len(self._terminal_sessions) < 2:
                    self._open_terminal(None, force_new=True)
                elif self._active_terminal_session() is not None:
                    self.mode = MODE_TERMINAL
                self._set_message(f"Terminal split {'on' if self._terminal_split_view else 'off'}.")
                return
            if action in {"list", "ls"}:
                if not self._terminal_sessions:
                    self._show_alert("(no terminal sessions)")
                    return
                lines: list[str] = []
                active_id = self._terminal_process_id
                for process_id in self._terminal_session_order:
                    session = self._terminal_sessions.get(process_id)
                    if session is None:
                        continue
                    marker = "*" if process_id == active_id else " "
                    state = self._process_manager.status(process_id)
                    lines.append(f"{marker} {process_id} [{state}] {session.command}")
                if not lines:
                    self._show_alert("(no terminal sessions)")
                    return
                self._show_alert("\n".join(lines))
                return
            if action == "use":
                if len(args) < 2:
                    self._set_message("Usage: :term use <id|next|prev>", error=True)
                    return
                token = args[1].strip().lower()
                if token in {"next", "n"}:
                    self._switch_terminal_relative(1)
                    return
                if token in {"prev", "p", "previous"}:
                    self._switch_terminal_relative(-1)
                    return
                try:
                    process_id = int(token)
                except ValueError:
                    self._set_message("Usage: :term use <id|next|prev>", error=True)
                    return
                if not self._switch_terminal_session(process_id):
                    self._set_message(f"Terminal session not found: {process_id}", error=True)
                    return
                self.mode = MODE_TERMINAL
                self._set_message(f"Terminal switched: {process_id}")
                return
            if action in {"next", "n"}:
                self._switch_terminal_relative(1)
                return
            if action in {"prev", "p", "previous"}:
                self._switch_terminal_relative(-1)
                return
            if action in {"close", "hide"}:
                self._close_terminal(kill=False)
                self._set_message("Terminal closed.")
                return
            if action in {"stop", "kill"}:
                if len(args) >= 2 and args[1].strip().lower() == "all":
                    process_ids = [pid for pid in self._terminal_session_order if pid in self._terminal_sessions]
                    if not process_ids:
                        self._set_message("No terminal sessions.", error=True)
                        return
                    for process_id in process_ids:
                        self._process_manager.stop_sync(process_id, timeout=0.6)
                        session = self._terminal_sessions.get(process_id)
                        if session is None:
                            continue
                        if process_id == self._dap_session_process_id:
                            self._dap_session_process_id = None
                            self._dap_target_path = None
                        session.output.append("[term] stop requested")
                        if len(session.output) > 2000:
                            session.output = session.output[-2000:]
                    self._set_message("Terminal stop requested for all sessions.")
                    return
                self._close_terminal(kill=True)
                self._set_message("Terminal stop requested.")
                return
            if action == "status":
                session = self._active_terminal_session()
                if session is None:
                    self._set_message("Terminal: idle")
                    return
                state = self._process_manager.status(session.process_id)
                self._set_message(
                    f"Terminal {session.process_id}: {state} "
                    f"(sessions={len(self._terminal_sessions)} split={'on' if self._terminal_split_view else 'off'})"
                )
                return
            if action == "history":
                count = 40
                if len(args) >= 2:
                    try:
                        count = max(1, int(args[1]))
                    except ValueError:
                        self._set_message("Usage: :term history [lines]", error=True)
                        return
                session = self._active_terminal_session()
                if session is None:
                    self._set_message("Terminal is not running.", error=True)
                    return
                lines = session.output[-count:]
                if not lines:
                    self._show_alert("(terminal history is empty)")
                    return
                self._show_alert("\n".join(lines))
                return
            if action == "search":
                if len(args) < 2:
                    self._set_message("Usage: :term search <query|next|prev|clear>", error=True)
                    return
                token = args[1].strip().lower()
                if token in {"next", "n"}:
                    self._terminal_search_shift(1)
                    return
                if token in {"prev", "p", "previous"}:
                    self._terminal_search_shift(-1)
                    return
                if token in {"clear", "cls"}:
                    self._clear_terminal_search()
                    return
                self._terminal_search(" ".join(args[1:]))
                return
            if action == "send":
                if len(args) < 2:
                    self._set_message("Usage: :term send <text>", error=True)
                    return
                self._send_terminal_input(" ".join(args[1:]))
                return
            if action in {"clear", "cls"}:
                if len(args) >= 2 and args[1].strip().lower() == "all":
                    for session in self._terminal_sessions.values():
                        session.output.clear()
                        session.search_hits = []
                        session.search_index = -1
                        session.search_query = ""
                    self._sync_active_terminal_refs()
                    self._set_message("Terminal history cleared (all sessions).")
                    return
                session = self._active_terminal_session()
                if session is None:
                    self._set_message("Terminal is not running.", error=True)
                    return
                session.output.clear()
                session.search_hits = []
                session.search_index = -1
                session.search_query = ""
                self._sync_active_terminal_refs()
                self._set_message("Terminal history cleared.")
                return
            self._set_message(
                "Usage: :term [open|new|split|list|use|next|prev|close|stop|status|history|search|send|clear] [args...]",
                error=True,
            )
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
                "Terminal "
                f"truecolor={caps.true_color} colors={caps.color_level} "
                f"unicode={caps.unicode_ui} hyperlink={caps.hyperlink} sixel={caps.sixel}"
            )
            return

        if cmd == "theme":
            action = args[0].strip() if args else "status"
            lowered = action.lower()
            if lowered in {"status", "current"}:
                self._set_message(f"Theme: {self.config.theme_file()}")
                return
            if lowered in {"list", "ls"}:
                records = self._theme_manager.list_themes()
                if not records:
                    self._show_alert("(no themes)")
                    return
                lines = [
                    (
                        f"{item.name} v{item.version}"
                        f" | {item.source}"
                        f" | {item.description or 'no description'}"
                        f" | preview={item.preview or '-'}"
                    )
                    for item in records
                ]
                self._show_alert("\n".join(lines))
                return
            if lowered == "install":
                if len(args) < 2:
                    self._set_message("Usage: :theme install <path>", error=True)
                    return
                source = self._resolve_path(args[1])
                try:
                    record = self._theme_manager.install(source)
                except Exception as exc:
                    self._set_message(f"Theme install failed: {exc}", error=True)
                    return
                self._set_message(f"Theme installed: {record.name} v{record.version}")
                return
            if lowered in {"uninstall", "remove", "rm"}:
                if len(args) < 2:
                    self._set_message("Usage: :theme uninstall <name>", error=True)
                    return
                try:
                    message = self._theme_manager.uninstall(args[1])
                except Exception as exc:
                    self._set_message(f"Theme uninstall failed: {exc}", error=True)
                    return
                self._set_message(message)
                return
            target = self._theme_manager.resolve(action)
            if target is None:
                candidates = []
                direct = self.config.resolve_path(action)
                if direct is not None:
                    candidates.append(direct)
                clean = action.replace("/", "\\")
                if not clean.lower().endswith(".json"):
                    themed = self.config.resolve_path(f"themes\\{clean}.json")
                    if themed is not None:
                        candidates.append(themed)
                    modern = self.config.resolve_path(f"themes\\pvim.theme.{clean}.json")
                    if modern is not None:
                        candidates.append(modern)
                target = next((item for item in candidates if item.exists() and item.is_file()), None)
            if target is None:
                self._set_message(f"Theme not found: {action}", error=True)
                return
            theme_section = self.config.data.get("theme")
            if not isinstance(theme_section, dict):
                theme_section = {}
                self.config.data["theme"] = theme_section
            theme_section["enabled"] = True
            theme_section["config_file"] = str(target)
            self._apply_runtime_config()
            self._set_message(f"Theme applied: {target.name}")
            return

        if cmd == "syntax":
            action = args[0].lower() if args else "reload"
            if action in {"reload", "refresh"}:
                syntax = self._syntax_manager()
                syntax.reload()
                self._syntax_profile = syntax.profile_for_file(self.file_path)
                self._set_message("Syntax profiles reloaded.")
                return
            self._set_message("Usage: :syntax reload", error=True)
            return

        if cmd == "git":
            action = args[0].lower() if args else "status"
            if action == "status":
                ok, payload = status_short(self._workspace_root)
                if not ok:
                    self._set_message(f"Git status failed: {payload}", error=True)
                    return
                lines = payload if isinstance(payload, list) else []
                self._show_alert("\n".join(lines) if lines else "(clean)")
                return
            if action == "branches":
                ok, payload = list_branches(self._workspace_root)
                if not ok:
                    self._set_message(f"Git branch list failed: {payload}", error=True)
                    return
                lines = payload if isinstance(payload, list) else []
                self._show_alert("\n".join(lines) if lines else "(no branches)")
                return
            if action == "checkout":
                if len(args) < 2:
                    self._set_message("Usage: :git checkout <branch>", error=True)
                    return
                ok, payload = checkout_branch(self._workspace_root, args[1])
                if not ok:
                    self._set_message(f"Git checkout failed: {payload}", error=True)
                    return
                self._set_message(payload or f"Checked out {args[1]}")
                return
            if self.file_path is None:
                self._set_message("Git file command requires an opened file.", error=True)
                return
            if action == "diff":
                staged = any(token.lower() in {"--staged", "--cached", "staged"} for token in args[1:])
                ok, payload = current_file_diff(self._workspace_root, self.file_path, staged=staged)
                if not ok:
                    self._set_message(f"Git diff failed: {payload}", error=True)
                    return
                self._show_alert(payload[:7000] if payload else "(no diff)")
                return
            if action == "blame":
                line = self.cy + 1
                if len(args) >= 2:
                    try:
                        line = max(1, int(args[1]))
                    except ValueError:
                        self._set_message("Usage: :git blame [line]", error=True)
                        return
                ok, payload = blame_line(self._workspace_root, self.file_path, line)
                if not ok:
                    self._set_message(f"Git blame failed: {payload}", error=True)
                    return
                rendered = "\n".join(payload.splitlines()[:40])
                self._show_alert(rendered[:7000] if rendered else "(no blame output)")
                return
            if action == "stage":
                ok, payload = stage_file(self._workspace_root, self.file_path)
                self._set_message("Git stage: ok" if ok else f"Git stage failed: {payload}", error=not ok)
                return
            if action == "unstage":
                ok, payload = unstage_file(self._workspace_root, self.file_path)
                self._set_message("Git unstage: ok" if ok else f"Git unstage failed: {payload}", error=not ok)
                return
            self._set_message("Usage: :git status|diff [--staged]|blame [line]|stage|unstage|branches|checkout <b>", error=True)
            return

        if cmd == "session":
            action = args[0].lower() if args else "save"
            if action == "save":
                if len(args) >= 2:
                    path = self._session_profile_path(args[1])
                    if path is None:
                        self._set_message("Session name must contain letters, numbers, '_' or '-'.", error=True)
                        return
                    if self._save_session_to(path):
                        self._set_message(f"Session saved: {path}")
                    else:
                        self._set_message(f"Session save failed: {path}", error=True)
                    return
                self._save_session()
                self._set_message(f"Session saved: {self._session_path}")
                return
            if action == "load":
                if len(args) >= 2:
                    path = self._session_profile_path(args[1])
                    if path is None:
                        self._set_message("Session name must contain letters, numbers, '_' or '-'.", error=True)
                        return
                    if self._restore_session_from(path):
                        self._set_message(f"Session restored: {path}")
                    else:
                        self._set_message(f"Session not found: {path}", error=True)
                    return
                self._restore_session()
                self._set_message("Session restored.")
                return
            if action in {"list", "ls"}:
                names = self._session_profile_names()
                rendered = "\n".join(names) if names else "(no saved profiles)"
                self._show_alert(rendered)
                return
            self._set_message("Usage: :session save [name] | :session load [name] | :session list", error=True)
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
                self._sidebar_manual_override = True
                self.show_sidebar = True
                self._set_message("Sidebar: on")
                return
            if option == "off":
                self._sidebar_manual_override = True
                self.show_sidebar = False
                self._set_message("Sidebar: off")
                return
            if option == "toggle":
                self._toggle_sidebar()
                return
            self._set_message("Usage: :sidebar on|off|toggle", error=True)
            return

        if cmd == "encoding":
            if not args:
                self._set_message(f"Encoding: {self._current_encoding}")
                return
            self._set_encoding(args[0])
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
            if action in {"status", "manager", "info"}:
                total = len(self._file_tree_feature.entries)
                files = sum(1 for item in self._file_tree_feature.entries if not item.is_dir)
                state = "visible" if self.mode == MODE_EXPLORER else "hidden"
                lines = [
                    "File Tree Manager",
                    "",
                    f"state: {state}",
                    f"workspace: {self._workspace_root}",
                    f"entries: {total} (files: {files})",
                    f"sort: {self._file_tree_feature.sort_mode()}",
                    f"filter: {self._file_tree_feature.filter_query() or '(none)'}",
                    f"show_hidden: {'on' if self._file_tree_feature.show_hidden() else 'off'}",
                    "",
                    "operations:",
                    "  :tree open|refresh|close|toggle",
                    "  :tree sort <name|type|mtime>",
                    "  :tree filter <query>|clear-filter",
                    "  :tree hidden <on|off>",
                ]
                self._show_alert("\n".join(lines))
                return
            if action == "sort":
                if len(args) < 2:
                    self._set_message("Usage: :tree sort <name|type|mtime>", error=True)
                    return
                mode = args[1].strip().lower()
                if not self._file_tree_feature.set_sort_mode(mode):
                    self._set_message("Usage: :tree sort <name|type|mtime>", error=True)
                    return
                if self.mode == MODE_EXPLORER:
                    self._sync_file_tree_popup()
                self._set_message(f"Tree sort: {mode}")
                return
            if action == "filter":
                query = " ".join(args[1:]).strip()
                self._file_tree_feature.set_filter_query(query)
                if self.mode == MODE_EXPLORER:
                    self._sync_file_tree_popup()
                if query:
                    self._set_message(f"Tree filter: {query}")
                else:
                    self._set_message("Tree filter cleared.")
                return
            if action in {"filter-clear", "clear-filter"}:
                self._file_tree_feature.set_filter_query("")
                if self.mode == MODE_EXPLORER:
                    self._sync_file_tree_popup()
                self._set_message("Tree filter cleared.")
                return
            if action == "hidden":
                if len(args) < 2:
                    self._set_message("Usage: :tree hidden <on|off>", error=True)
                    return
                option = args[1].strip().lower()
                if option not in {"on", "off"}:
                    self._set_message("Usage: :tree hidden <on|off>", error=True)
                    return
                self._file_tree_feature.set_show_hidden(option == "on")
                self._schedule_file_tree_refresh()
                self._set_message(f"Tree hidden files: {option}")
                return
            self._set_message("Usage: :tree open|refresh|close|toggle|sort|filter|clear-filter|hidden", error=True)
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
