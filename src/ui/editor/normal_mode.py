from __future__ import annotations

from .modes import MODE_INSERT, MODE_NORMAL, MODE_VISUAL


class NormalModeMixin:
    def _handle_normal_key(self, key: str) -> None:
        if self._macro_waiting_action:
            action = self._macro_waiting_action
            self._macro_waiting_action = ""
            if len(key) == 1 and key.isalpha():
                register = key.lower()
                if action == "record":
                    self._start_macro_recording(register)
                else:
                    self._replay_macro(register)
            else:
                self._set_message("Macro register must be [a-z].", error=True)
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if self.pending_scope:
            operator = self.pending_operator or "d"
            handled = self._apply_text_object(operator, self.pending_scope, key)
            if not handled:
                self._set_message(f"Unknown text object: {self.pending_scope}{key}", error=True)
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if self._pending_motion == "g":
            self._pending_motion = ""
            if key == "d":
                self._goto_definition()
            elif key == "g":
                self.cy = 0
                self.cx = min(self.cx, len(self._line()))
            elif key == "a":
                self._open_code_actions()
            elif key == "-":
                self._undo_branch_prev()
            elif key == "+":
                self._undo_branch_next()
            elif key == "b":
                self._jump_back()
            elif key == "f":
                self._jump_forward()
            else:
                self._set_message(f"Unknown g motion: g{key}", error=True)
            return

        if self._pending_motion == "CTRL_W":
            self._pending_motion = ""
            if key in {"v", "V"}:
                self._open_split("vertical")
            elif key in {"s", "S"}:
                self._open_split("horizontal")
            elif key in {"w", "W"}:
                self._toggle_split_focus()
            elif key in {"h", "H"}:
                self._resize_split(-0.05)
            elif key in {"l", "L"}:
                self._resize_split(0.05)
            elif key in {"q", "Q", "c", "C"}:
                self._close_split()
            else:
                self._set_message(f"Unknown window command: <C-w>{key}", error=True)
            return

        if key in {"h", "LEFT"}:
            self._move_left()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key in {"l", "RIGHT"}:
            self._move_right()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key in {"k", "UP"}:
            self._move_up()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key in {"j", "DOWN"}:
            self._move_down()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key in {"HOME", "0"}:
            self.cx = 0
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return
        if key in {"END", "$"}:
            self.cx = len(self._line())
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "G":
            self.cy = max(0, len(self.lines) - 1)
            self.cx = min(self.cx, len(self._line()))
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "PGUP":
            self._page_up()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "PGDN":
            self._page_down()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if self.pending_operator in {"d", "c", "v"} and key in {"a", "i"}:
            self.pending_scope = key
            self._set_message(f"{self.pending_operator}{key}")
            return

        if key in {"i"}:
            self.mode = MODE_INSERT
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            self._set_message("-- INSERT --")
            return

        if key == "I":
            line = self._line()
            self.cx = len(line) - len(line.lstrip(" \t"))
            self.mode = MODE_INSERT
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            self._set_message("-- INSERT --")
            return

        if key in {"a"}:
            self.cx = min(self.cx + 1, len(self._line()))
            self.mode = MODE_INSERT
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            self._set_message("-- INSERT --")
            return

        if key in {"A"}:
            self.cx = len(self._line())
            self.mode = MODE_INSERT
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            self._set_message("-- INSERT --")
            return

        if key in {"o"}:
            self._open_line_below()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "O":
            self._open_line_above()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key in {"x", "DEL"}:
            self._delete_char()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key in {"d", "c", "v"}:
            if key == "d" and self.pending_operator == "d":
                self._delete_line()
                self.pending_operator = ""
                self.pending_scope = ""
                self._pending_motion = ""
            elif key == "c" and self.pending_operator == "c":
                self._delete_line()
                self.mode = MODE_INSERT
                self.pending_operator = ""
                self.pending_scope = ""
                self._pending_motion = ""
                self._set_message("-- INSERT --")
            else:
                self.pending_operator = key
                self.pending_scope = ""
                self._pending_motion = ""
                self._set_message(key)
            return

        if key == "u":
            self._undo()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "CTRL_Y":
            self._redo()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "CTRL_R":
            self._redo()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "CTRL_O":
            self._jump_back()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "q":
            if self._macro_recording_register is not None:
                self._stop_macro_recording()
                return
            if not self.config.macros_enabled():
                self._set_message("Macros are disabled in config.", error=True)
                return
            self._macro_waiting_action = "record"
            self._set_message("Macro record: choose register [a-z]")
            return

        if key == "@":
            self._macro_waiting_action = "play"
            self._set_message("Macro replay: choose register [a-z]")
            return

        if key == "g":
            self._pending_motion = "g"
            self.pending_operator = ""
            self.pending_scope = ""
            self._set_message("g")
            return

        if key == "CTRL_W":
            self._pending_motion = "CTRL_W"
            self.pending_operator = ""
            self.pending_scope = ""
            self._set_message("<C-w>")
            return

        if key == "K":
            self._show_hover()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "%":
            self._jump_to_matching_bracket()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == ":":
            self._enter_command()
            return

        if key == "/":
            self._enter_command("find ", prompt="/")
            return

        if key == "n":
            if self._last_search_query:
                self._find(self._last_search_query)
            else:
                self._set_message("No previous search pattern.")
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "V":
            self.mode = MODE_VISUAL
            self.visual_anchor = self.cy
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            self._set_message("-- VISUAL LINE --")
            return

        if key == ">":
            self._indent_lines(self.cy, self.cy)
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "<":
            self._outdent_lines(self.cy, self.cy)
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "F2":
            self.show_line_numbers = not self.show_line_numbers
            state = "on" if self.show_line_numbers else "off"
            self._set_message(f"Line numbers: {state}")
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "F4":
            self._toggle_sidebar()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            return

        if key == "ESC":
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
            self._clear_multi_cursor()
