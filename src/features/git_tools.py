from __future__ import annotations

import subprocess
from pathlib import Path


def _run_git(cwd: Path, *args: str, timeout: float = 1.5) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, "git command timed out"

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git exited with code {result.returncode}"
        return False, message
    return True, result.stdout.strip()


def current_file_diff(cwd: Path, file_path: Path, *, staged: bool = False, context_lines: int = 3) -> tuple[bool, str]:
    args = ["diff", "--no-color", f"--unified={max(0, int(context_lines))}"]
    if staged:
        args.append("--staged")
    args.extend(["--", str(file_path)])
    return _run_git(cwd, *args, timeout=2.0)


def blame_line(cwd: Path, file_path: Path, line_number: int) -> tuple[bool, str]:
    line = max(1, int(line_number))
    return _run_git(
        cwd,
        "blame",
        "--line-porcelain",
        f"-L{line},{line}",
        "--",
        str(file_path),
        timeout=2.0,
    )


def stage_file(cwd: Path, file_path: Path) -> tuple[bool, str]:
    ok, out = _run_git(cwd, "add", "--", str(file_path))
    if not ok:
        return False, out
    return True, "staged"


def unstage_file(cwd: Path, file_path: Path) -> tuple[bool, str]:
    ok, out = _run_git(cwd, "restore", "--staged", "--", str(file_path))
    if not ok:
        return False, out
    return True, "unstaged"


def status_short(cwd: Path) -> tuple[bool, list[str] | str]:
    ok, out = _run_git(cwd, "status", "--short", "--branch", timeout=2.0)
    if not ok:
        return False, out
    lines = [line for line in out.splitlines() if line.strip()]
    return True, lines


def list_branches(cwd: Path) -> tuple[bool, list[str] | str]:
    ok, out = _run_git(cwd, "branch", "--list", timeout=2.0)
    if not ok:
        return False, out
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return True, lines


def checkout_branch(cwd: Path, branch: str) -> tuple[bool, str]:
    clean = branch.strip()
    if not clean:
        return False, "branch name is empty"
    return _run_git(cwd, "checkout", clean, timeout=3.0)
