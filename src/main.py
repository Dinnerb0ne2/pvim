from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Iterable

from . import APP_NAME, APP_VERSION
from .core.config import AppConfig
from .ui.editor import PvimEditor


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pvim",
        description=f"{APP_NAME} {APP_VERSION} - modular Vim-style editor (stdlib only)",
    )
    parser.add_argument("file", nargs="?", help="Optional file or folder path to open.")
    parser.add_argument(
        "--config",
        default="pvim.config.json",
        help="Path to editor config file. Default: pvim.config.json",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return parser.parse_args(argv)


def _parse_version_tuple(value: str) -> tuple[int, int, int] | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def main(argv: Iterable[str] | None = None) -> None:
    args = parse_args(argv)
    config_path = Path(args.config).expanduser()

    try:
        config = AppConfig.load(config_path)
    except Exception as exc:
        raise SystemExit(f"Config load failed: {exc}") from exc

    if config.experimental_jit_enabled():
        os.environ.setdefault("PYTHON_JIT", "1")

    required = _parse_version_tuple(config.required_python())
    if required is not None and sys.version_info[:3] < required:
        print(
            f"Warning: target Python is {config.required_python()}, current is "
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}.",
            file=sys.stderr,
        )

    file_path = Path(args.file).expanduser() if args.file else None
    editor = PvimEditor(file_path=file_path, config=config)
    editor.run()
