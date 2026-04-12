from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re

HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(slots=True, frozen=True)
class GitSnapshot:
    branch: str
    file_state: str
    line_markers: dict[int, str]


class GitControlFeature:
    __slots__ = ("enabled", "branch", "file_state", "line_markers")

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.branch = "-"
        self.file_state = "clean"
        self.line_markers: dict[int, str] = {}

    async def collect(self, cwd: Path, file_path: Path | None) -> GitSnapshot:
        if not self.enabled:
            return GitSnapshot(branch="-", file_state="clean", line_markers={})
        branch = await self._branch(cwd)
        file_state = await self._file_state(cwd, file_path)
        line_markers = await self._line_markers(cwd, file_path)
        return GitSnapshot(branch=branch, file_state=file_state, line_markers=line_markers)

    def apply(self, snapshot: GitSnapshot) -> None:
        self.branch = snapshot.branch
        self.file_state = snapshot.file_state
        self.line_markers = snapshot.line_markers

    def status_segment(self) -> str:
        if not self.enabled:
            return ""
        return f"git:{self.branch} {self.file_state}"

    async def _branch(self, cwd: Path) -> str:
        output = await self._run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        return output or "-"

    async def _file_state(self, cwd: Path, file_path: Path | None) -> str:
        if file_path is None:
            return "clean"
        output = await self._run_git(cwd, "status", "--porcelain", "--", str(file_path))
        line = output.splitlines()[0] if output else ""
        return line[:2].strip() or "clean"

    async def _line_markers(self, cwd: Path, file_path: Path | None) -> dict[int, str]:
        if file_path is None:
            return {}
        output = await self._run_git(cwd, "diff", "--no-color", "--unified=0", "--", str(file_path))
        markers: dict[int, str] = {}
        for line in output.splitlines():
            if not line.startswith("@@"):
                continue
            match = HUNK_RE.search(line)
            if match is None:
                continue
            start_line = int(match.group(1))
            count = int(match.group(2) or "1")
            for row in range(start_line, start_line + max(1, count)):
                markers[row] = "+"
        return markers

    async def _run_git(self, cwd: Path, *args: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "--no-pager",
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await process.communicate()
        except OSError:
            return ""
        if process.returncode != 0:
            return ""
        return stdout.decode("utf-8", errors="replace").strip()
