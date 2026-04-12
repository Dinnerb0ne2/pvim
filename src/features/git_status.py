from __future__ import annotations

import subprocess
import time
from pathlib import Path


class GitStatusProvider:
    def __init__(self, cwd: Path, *, enabled: bool, refresh_seconds: float) -> None:
        self._enabled = enabled
        self._cwd = cwd.resolve()
        self._refresh_seconds = max(0.2, refresh_seconds)
        self._repo_root: Path | None = None
        self._branch = "-"
        self._statuses: dict[str, str] = {}
        self._last_refresh = 0.0
        self._detected = False

    def _run_git(self, *args: str, cwd: Path | None = None) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd or self._cwd),
                capture_output=True,
                text=True,
                timeout=0.8,
                check=False,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _detect_repo(self) -> None:
        if self._detected:
            return
        self._detected = True
        if not self._enabled:
            return

        root = self._run_git("rev-parse", "--show-toplevel")
        if not root:
            return
        self._repo_root = Path(root).resolve()

    def refresh_if_needed(self, *, force: bool = False) -> None:
        self._detect_repo()
        if self._repo_root is None:
            return

        now = time.monotonic()
        if not force and now - self._last_refresh < self._refresh_seconds:
            return
        self._last_refresh = now

        branch = self._run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=self._repo_root)
        if branch:
            self._branch = branch

        porcelain = self._run_git("status", "--porcelain", "--untracked-files=all", cwd=self._repo_root)
        statuses: dict[str, str] = {}
        if porcelain:
            for line in porcelain.splitlines():
                if len(line) < 3:
                    continue
                status = line[:2]
                path_part = line[3:].strip()
                if " -> " in path_part:
                    path_part = path_part.split(" -> ", maxsplit=1)[1].strip()
                normalized = path_part.replace("\\", "/")
                statuses[normalized] = self._status_marker(status)
        self._statuses = statuses

    def _status_marker(self, status: str) -> str:
        if status == "??":
            return "?"
        if "M" in status:
            return "M"
        if "A" in status:
            return "A"
        if "D" in status:
            return "D"
        if "R" in status:
            return "R"
        if "C" in status:
            return "C"
        if "U" in status:
            return "U"
        return " "

    def branch_label(self, file_path: Path | None) -> str:
        self.refresh_if_needed()
        if self._repo_root is None:
            return "-"
        marker = self.status_for_file(file_path)
        if marker.strip():
            return f"{self._branch} [{marker}]"
        return self._branch

    def status_for_relative(self, relative_path: Path) -> str:
        self.refresh_if_needed()
        if self._repo_root is None:
            return " "
        key = str(relative_path).replace("\\", "/")
        return self._statuses.get(key, " ")

    def status_for_file(self, file_path: Path | None) -> str:
        self.refresh_if_needed()
        if self._repo_root is None or file_path is None:
            return " "

        try:
            relative = file_path.resolve().relative_to(self._repo_root)
        except ValueError:
            return " "
        return self.status_for_relative(relative)
