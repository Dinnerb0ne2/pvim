from __future__ import annotations

from .modes import MODE_INSERT, MODE_NORMAL, MODE_TERMINAL


class UIModeMixin:
    def _handle_command_key(self, key: str) -> None:
        if key == "ESC":
            self.mode = MODE_NORMAL
            self.command_text = ""
            self._command_prompt = ":"
            self._set_message("Command cancelled.")
            return

        if key == "BACKSPACE":
            self.command_text = self.command_text[:-1]
            return

        if key == "ENTER":
            command = self.command_text
            self.command_text = ""
            self.mode = MODE_NORMAL
            self._command_prompt = ":"
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
        if self._swap_prompt_payload is not None:
            if key in {"y", "Y"}:
                self._apply_swap_payload()
                self._clear_swap_prompt()
                return
            if key in {"n", "N", "ESC"}:
                if self._swap_prompt_path is not None:
                    self._persistence.remove_swap(self._swap_prompt_path)
                self._set_message("Swap discarded.")
                self._clear_swap_prompt()
                return
        if key in {"ESC", "ENTER"}:
            self._close_alert()

    def _handle_terminal_key(self, key: str) -> None:
        if key == "ESC":
            self._close_terminal(kill=False)
            return
        if key == "CTRL_C":
            self._send_terminal_input("\u0003")
            return
        if key == "CTRL_D":
            self._send_terminal_input("\u0004")
            return
        if key == "PGUP":
            self._terminal_scroll = min(self._terminal_scroll + 1, max(0, len(self._terminal_output) - 1))
            return
        if key == "PGDN":
            self._terminal_scroll = max(0, self._terminal_scroll - 1)
            return
        if key == "BACKSPACE":
            self._terminal_input = self._terminal_input[:-1]
            return
        if key == "ENTER":
            self._send_terminal_input(self._terminal_input)
            self._terminal_input = ""
            self._terminal_scroll = 0
            return
        if key == "TAB":
            self._terminal_input += "\t"
            return
        if len(key) == 1 and key.isprintable():
            self._terminal_input += key
            return
        if key == "CTRL_Q":
            self._close_terminal(kill=True)

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
            if self._floating_source == "live_grep":
                self._accept_live_grep_selection()
                return
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
            if self.open_file(self._workspace_root / selected, force=False):
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
