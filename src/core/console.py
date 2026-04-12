from __future__ import annotations

import ctypes
import os
import sys

if os.name != "nt":
    raise SystemExit("PVI 目前仅支持 Windows 终端（纯标准库实现）。")

import msvcrt

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
    "\x15": "CTRL_U",
    "\x10": "CTRL_P",
    "\x1f": "CTRL_SLASH",
}


class ConsoleController:
    STD_OUTPUT_HANDLE = -11
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    def __init__(self) -> None:
        self._kernel32 = ctypes.windll.kernel32
        self._output_handle = self._kernel32.GetStdHandle(self.STD_OUTPUT_HANDLE)
        self._original_mode: int | None = None

    def enter(self) -> None:
        mode = ctypes.c_uint()
        if not self._kernel32.GetConsoleMode(self._output_handle, ctypes.byref(mode)):
            raise RuntimeError("无法读取终端模式。")

        self._original_mode = mode.value
        new_mode = mode.value | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING | self.DISABLE_NEWLINE_AUTO_RETURN
        if not self._kernel32.SetConsoleMode(self._output_handle, new_mode):
            raise RuntimeError("终端不支持 VT 模式。")

        sys.stdout.write(f"{CSI}?1049h{CSI}2J{CSI}H")
        sys.stdout.flush()

    def exit(self) -> None:
        try:
            if self._original_mode is not None:
                self._kernel32.SetConsoleMode(self._output_handle, self._original_mode)
        finally:
            sys.stdout.write(f"{RESET}{CSI}?1049l")
            sys.stdout.flush()


class KeyReader:
    @staticmethod
    def has_key() -> bool:
        return bool(msvcrt.kbhit())

    @staticmethod
    def read_key() -> str:
        key = msvcrt.getwch()
        if key in ("\x00", "\xe0"):
            return SPECIAL_KEYS.get(msvcrt.getwch(), "UNKNOWN")
        return CONTROL_KEYS.get(key, key)
