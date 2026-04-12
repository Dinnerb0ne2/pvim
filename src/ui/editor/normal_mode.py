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
            else:
                self._set_message(f"Unknown g motion: g{key}", error=True)
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

        if key == "K":
            self._show_hover()
            self.pending_operator = ""
            self.pending_scope = ""
            self._pending_motion = ""
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
