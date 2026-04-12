from __future__ import annotations

import os
import sys
import time

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    import msvcrt
else:
    import select
    import termios
    import tty

CSI = "\x1b["
RESET = f"{CSI}0m"

SPECIAL_KEYS = {
    ";": "F1",
    "H": "UP",
    "P": "DOWN",
    "K": "LEFT",
    "M": "RIGHT",
    "G": "HOME",
    "O": "END",
    "I": "PGUP",
    "Q": "PGDN",
    "S": "DEL",
    "<": "F2",
    "=": "F3",
    ">": "F4",
    "?": "F5",
    "@": "F6",
    "A": "F7",
    "B": "F8",
    "C": "F9",
    "D": "F10",
    "Z": "SHIFT_TAB",
    "s": "CTRL_LEFT",
    "t": "CTRL_RIGHT",
}

CONTROL_KEYS = {
    "\r": "ENTER",
    "\x1b": "ESC",
    "\x08": "BACKSPACE",
    "\t": "TAB",
    "\x13": "CTRL_S",
    "\x11": "CTRL_Q",
    "\x03": "CTRL_C",
    "\x06": "CTRL_F",
    "\x07": "CTRL_G",
    "\x04": "CTRL_D",
    "\x12": "CTRL_R",
    "\x19": "CTRL_Y",
    "\x15": "CTRL_U",
    "\x0e": "CTRL_N",
    "\x10": "CTRL_P",
    "\x1f": "CTRL_SLASH",
}


class ConsoleController:
    if IS_WINDOWS:
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    def __init__(self) -> None:
        if IS_WINDOWS:
            self._kernel32 = ctypes.windll.kernel32
            self._output_handle = self._kernel32.GetStdHandle(self.STD_OUTPUT_HANDLE)
            self._original_mode: int | None = None
        else:
            self._input_fd = sys.stdin.fileno()
            self._original_mode: list[int] | None = None

    def enter(self) -> None:
        if IS_WINDOWS:
            mode = ctypes.c_uint()
            if not self._kernel32.GetConsoleMode(self._output_handle, ctypes.byref(mode)):
                raise RuntimeError("无法读取终端模式。")

            self._original_mode = mode.value
            new_mode = mode.value | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING | self.DISABLE_NEWLINE_AUTO_RETURN
            if not self._kernel32.SetConsoleMode(self._output_handle, new_mode):
                raise RuntimeError("终端不支持 VT 模式。")
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise RuntimeError("当前终端不支持原始模式输入。")
            self._original_mode = termios.tcgetattr(self._input_fd)
            tty.setraw(self._input_fd)

        sys.stdout.write(f"{CSI}?1049h{CSI}2J{CSI}H")
        sys.stdout.flush()

    def exit(self) -> None:
        try:
            if IS_WINDOWS:
                if self._original_mode is not None:
                    self._kernel32.SetConsoleMode(self._output_handle, self._original_mode)
            else:
                if self._original_mode is not None:
                    termios.tcsetattr(self._input_fd, termios.TCSADRAIN, self._original_mode)
        finally:
            sys.stdout.write(f"{RESET}{CSI}?1049l")
            sys.stdout.flush()


POSIX_SPECIAL_KEYS = {
    "[A": "UP",
    "[B": "DOWN",
    "[C": "RIGHT",
    "[D": "LEFT",
    "[H": "HOME",
    "[F": "END",
    "[1~": "HOME",
    "[4~": "END",
    "[5~": "PGUP",
    "[6~": "PGDN",
    "[3~": "DEL",
    "[Z": "SHIFT_TAB",
    "[1;5D": "CTRL_LEFT",
    "[1;5C": "CTRL_RIGHT",
    "[5D": "CTRL_LEFT",
    "[5C": "CTRL_RIGHT",
    "OP": "F1",
    "OQ": "F2",
    "OR": "F3",
    "OS": "F4",
    "[11~": "F1",
    "[12~": "F2",
    "[13~": "F3",
    "[14~": "F4",
    "[15~": "F5",
    "[17~": "F6",
    "[18~": "F7",
    "[19~": "F8",
    "[20~": "F9",
    "[21~": "F10",
    "[23~": "F11",
    "[24~": "F12",
}


def _stdin_ready(timeout: float = 0.0) -> bool:
    if IS_WINDOWS:
        return bool(msvcrt.kbhit())
    ready, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout))
    return bool(ready)


def _read_escape_sequence_posix() -> str:
    sequence = ""
    deadline = time.monotonic() + 0.02
    while len(sequence) < 8 and time.monotonic() < deadline:
        if not _stdin_ready(0.001):
            break
        chunk = sys.stdin.read(1)
        if not chunk:
            break
        sequence += chunk
    return sequence


class KeyReader:
    @staticmethod
    def has_key() -> bool:
        return _stdin_ready(0.0)

    @staticmethod
    def read_key() -> str:
        if IS_WINDOWS:
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                return SPECIAL_KEYS.get(msvcrt.getwch(), "UNKNOWN")
            return CONTROL_KEYS.get(key, key)

        key = sys.stdin.read(1)
        if not key:
            return "UNKNOWN"
        if key == "\x1b":
            sequence = _read_escape_sequence_posix()
            if not sequence:
                return "ESC"
            return POSIX_SPECIAL_KEYS.get(sequence, "UNKNOWN")
        return CONTROL_KEYS.get(key, key)
