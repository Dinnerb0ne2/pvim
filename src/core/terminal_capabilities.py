from __future__ import annotations

from dataclasses import dataclass
import os
import sys


@dataclass(slots=True, frozen=True)
class TerminalCapabilities:
    true_color: bool
    color_level: int
    unicode_ui: bool


def detect_terminal_capabilities() -> TerminalCapabilities:
    colorterm = os.environ.get("COLORTERM", "").lower()
    term = os.environ.get("TERM", "").lower()
    force_ascii = os.environ.get("PVIM_ASCII_UI", "").strip() == "1"

    true_color = "truecolor" in colorterm or "24bit" in colorterm
    if true_color:
        color_level = 24
    elif "256color" in term:
        color_level = 256
    else:
        color_level = 16

    encoding = (sys.stdout.encoding or "").lower()
    unicode_ui = not force_ascii and ("utf" in encoding)
    return TerminalCapabilities(true_color=true_color, color_level=color_level, unicode_ui=unicode_ui)
