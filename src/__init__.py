from __future__ import annotations

from pathlib import Path
import subprocess

APP_NAME = "PVIM"
_BASE_MAJOR = 0
_BASE_MINOR = 7
_BASE_PATCH = 0


def _git_commit_count() -> int | None:
    root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=0.3,
            check=False,
        )
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text.isdigit():
        return None
    return int(text)


def _resolve_version() -> str:
    count = _git_commit_count()
    if count is None:
        return f"{_BASE_MAJOR}.{_BASE_MINOR}.{_BASE_PATCH}"
    patch = max(_BASE_PATCH, count - 1)
    return f"{_BASE_MAJOR}.{_BASE_MINOR}.{patch}"


APP_VERSION = _resolve_version()
