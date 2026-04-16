from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class GrepMatch:
    file_path: Path
    line: int
    column: int
    text: str

    def label(self, root: Path) -> str:
        try:
            relative = self.file_path.resolve().relative_to(root.resolve())
        except ValueError:
            relative = self.file_path
        return f"{relative}:{self.line}:{self.column} {self.text}"


class LiveGrep:
    __slots__ = ()

    async def search(self, root: Path, query: str, *, limit: int = 200) -> list[GrepMatch]:
        clean = query.strip()
        if not clean:
            return []

        command = [
            "rg",
            "--line-number",
            "--column",
            "--no-heading",
            "--color",
            "never",
            clean,
            str(root),
        ]
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await process.communicate()
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await process.wait()
                except OSError:
                    pass
            raise
        except OSError:
            return self._search_fallback(root, clean, limit=limit)

        lines = stdout.decode("utf-8", errors="replace").splitlines()
        matches: list[GrepMatch] = []
        for line in lines:
            parsed = self._parse_line(line)
            if parsed is None:
                continue
            matches.append(parsed)
            if len(matches) >= limit:
                break
        if matches:
            return matches
        return self._search_fallback(root, clean, limit=limit)

    def _parse_line(self, text: str) -> GrepMatch | None:
        parts = text.split(":", 3)
        if len(parts) < 4:
            return None
        file_part, line_part, column_part, content = parts
        try:
            line = int(line_part)
            column = int(column_part)
        except ValueError:
            return None
        return GrepMatch(file_path=Path(file_part), line=line, column=column, text=content)

    def _search_fallback(self, root: Path, query: str, *, limit: int) -> list[GrepMatch]:
        needle = query.lower()
        matches: list[GrepMatch] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_index, line in enumerate(content.splitlines(), start=1):
                col = line.lower().find(needle)
                if col < 0:
                    continue
                matches.append(GrepMatch(file_path=path, line=line_index, column=col + 1, text=line.strip()))
                if len(matches) >= limit:
                    return matches
        return matches
