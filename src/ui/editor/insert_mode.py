from __future__ import annotations

from .modes import MODE_NORMAL


class InsertModeMixin:
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
            if key in {".", ">", ":"}:
                self._open_tab_completion()
