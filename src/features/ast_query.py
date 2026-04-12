from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class AstNodeRange:
    kind: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def to_compact(self) -> str:
        return f"{self.kind}:{self.start_line}:{self.start_col}:{self.end_line}:{self.end_col}"


class AstQueryService:
    def __init__(self) -> None:
        self._parser_cache: dict[str, Any] = {}
        self._tree_sitter_ready: bool | None = None
        self._get_parser: Any = None

    def query_at(
        self,
        *,
        file_path: Path | None,
        source: str,
        row: int,
        col: int,
        kinds: set[str] | None = None,
    ) -> AstNodeRange | None:
        safe_row = max(1, int(row))
        safe_col = max(1, int(col))
        selected_kinds = kinds or {"function", "class"}

        language = self._language_from_path(file_path)
        if language is not None and self._ensure_tree_sitter():
            found = self._query_tree_sitter(
                language=language,
                source=source,
                row=safe_row,
                col=safe_col,
                kinds=selected_kinds,
            )
            if found is not None:
                return found

        if language in {None, "python"}:
            return self._query_python_ast(source=source, row=safe_row, col=safe_col, kinds=selected_kinds)
        return None

    def _ensure_tree_sitter(self) -> bool:
        if self._tree_sitter_ready is not None:
            return self._tree_sitter_ready
        try:
            from tree_sitter_languages import get_parser  # type: ignore
        except Exception:
            self._tree_sitter_ready = False
            self._get_parser = None
            return False
        self._tree_sitter_ready = True
        self._get_parser = get_parser
        return True

    def _query_tree_sitter(
        self,
        *,
        language: str,
        source: str,
        row: int,
        col: int,
        kinds: set[str],
    ) -> AstNodeRange | None:
        parser = self._parser_cache.get(language)
        if parser is None:
            try:
                parser = self._get_parser(language)
            except Exception:
                return None
            self._parser_cache[language] = parser

        try:
            tree = parser.parse(source.encode("utf-8"))
        except Exception:
            return None

        target_row = row - 1
        target_col = col - 1
        node = self._find_smallest_match(tree.root_node, target_row, target_col, kinds)
        if node is None:
            return None
        kind = self._normalize_tree_sitter_kind(str(node.type))
        return AstNodeRange(
            kind=kind,
            start_line=int(node.start_point[0]) + 1,
            start_col=int(node.start_point[1]) + 1,
            end_line=int(node.end_point[0]) + 1,
            end_col=int(node.end_point[1]) + 1,
        )

    def _find_smallest_match(
        self,
        node: Any,
        row: int,
        col: int,
        kinds: set[str],
    ) -> Any | None:
        if not self._contains_point(node.start_point, node.end_point, row, col):
            return None

        best: Any | None = None
        node_kind = self._normalize_tree_sitter_kind(str(node.type))
        if node_kind in kinds:
            best = node

        for child in node.children:
            candidate = self._find_smallest_match(child, row, col, kinds)
            if candidate is None:
                continue
            if best is None:
                best = candidate
                continue
            best_size = self._node_span(best)
            candidate_size = self._node_span(candidate)
            if candidate_size < best_size:
                best = candidate
        return best

    def _contains_point(
        self,
        start_point: tuple[int, int],
        end_point: tuple[int, int],
        row: int,
        col: int,
    ) -> bool:
        start_row, start_col = start_point
        end_row, end_col = end_point
        if row < start_row or row > end_row:
            return False
        if row == start_row and col < start_col:
            return False
        if row == end_row and col > end_col:
            return False
        return True

    def _node_span(self, node: Any) -> tuple[int, int, int, int]:
        return (
            int(node.start_point[0]),
            int(node.start_point[1]),
            int(node.end_point[0]),
            int(node.end_point[1]),
        )

    def _normalize_tree_sitter_kind(self, kind: str) -> str:
        lowered = kind.lower()
        if "class" in lowered:
            return "class"
        if "function" in lowered or "method" in lowered:
            return "function"
        return lowered

    def _query_python_ast(self, *, source: str, row: int, col: int, kinds: set[str]) -> AstNodeRange | None:
        try:
            root = ast.parse(source)
        except SyntaxError:
            return None

        best: AstNodeRange | None = None
        for node in ast.walk(root):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "function"
            elif isinstance(node, ast.ClassDef):
                kind = "class"
            else:
                continue

            if kind not in kinds:
                continue

            start_line = int(getattr(node, "lineno", 0) or 0)
            start_col = int(getattr(node, "col_offset", 0) or 0) + 1
            end_line = int(getattr(node, "end_lineno", 0) or 0)
            end_col = int(getattr(node, "end_col_offset", 0) or 0) + 1
            if start_line <= 0 or end_line <= 0:
                continue

            if not self._contains_cursor(start_line, end_line, start_col, end_col, row, col):
                continue

            current = AstNodeRange(
                kind=kind,
                start_line=start_line,
                start_col=start_col,
                end_line=end_line,
                end_col=end_col,
            )
            if best is None or self._py_span_size(current) < self._py_span_size(best):
                best = current
        return best

    def _contains_cursor(
        self,
        start_line: int,
        end_line: int,
        start_col: int,
        end_col: int,
        row: int,
        col: int,
    ) -> bool:
        if row < start_line or row > end_line:
            return False
        if row == start_line and col < start_col:
            return False
        if row == end_line and col > end_col:
            return False
        return True

    def _py_span_size(self, value: AstNodeRange) -> tuple[int, int]:
        return (value.end_line - value.start_line, value.end_col - value.start_col)

    def _language_from_path(self, path: Path | None) -> str | None:
        if path is None:
            return None
        suffix = path.suffix.lower()
        if suffix == ".py":
            return "python"
        if suffix in {".js", ".mjs", ".cjs"}:
            return "javascript"
        if suffix == ".ts":
            return "typescript"
        if suffix in {".tsx"}:
            return "tsx"
        if suffix in {".json"}:
            return "json"
        return None
