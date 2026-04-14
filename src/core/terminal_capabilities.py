from __future__ import annotations

from dataclasses import dataclass
import os
import sys


@dataclass(slots=True, frozen=True)
class TerminalCapabilities:
    true_color: bool
    color_level: int
    unicode_ui: bool
    hyperlink: bool
    sixel: bool


def detect_terminal_capabilities() -> TerminalCapabilities:
    colorterm = os.environ.get("COLORTERM", "").lower()
    term = os.environ.get("TERM", "").lower()
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    no_color = os.environ.get("NO_COLOR", "").strip() != ""
    force_ascii = os.environ.get("PVIM_ASCII_UI", "").strip() == "1"

    true_color = (
        ("truecolor" in colorterm)
        or ("24bit" in colorterm)
        or ("truecolor" in term)
        or ("direct" in term)
        or bool(os.environ.get("WT_SESSION"))
        or term_program in {"wezterm", "vscode", "iterm.app"}
    )
    if no_color:
        color_level = 0
    elif true_color:
        color_level = 24
    elif "256color" in term:
        color_level = 256
    else:
        color_level = 16

    encoding = (sys.stdout.encoding or "").lower()
    unicode_ui = not force_ascii and (
        ("utf" in encoding)
        or bool(os.environ.get("WT_SESSION"))
        or term_program in {"wezterm", "vscode", "iterm.app"}
    )
    hyperlink = bool(
        os.environ.get("WT_SESSION")
        or term_program in {"wezterm", "vscode", "iterm.app", "apple_terminal"}
        or "kitty" in term
        or bool(os.environ.get("KONSOLE_VERSION"))
    )
    sixel = (
        "sixel" in term
        or "mlterm" in term
        or os.environ.get("TERM_PROGRAM", "").lower().startswith("mintty")
        or os.environ.get("XTERM_VERSION", "").lower().startswith("xterm")
    )
    return TerminalCapabilities(
        true_color=(False if no_color else true_color),
        color_level=color_level,
        unicode_ui=unicode_ui,
        hyperlink=hyperlink,
        sixel=sixel,
    )
