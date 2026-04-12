from __future__ import annotations


class ScriptError(Exception):
    def __init__(self, message: str, *, line: int) -> None:
        super().__init__(message)
        self.message = message
        self.line = max(1, line)

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


class ScriptLexError(ScriptError):
    """Lexing error."""


class ScriptParseError(ScriptError):
    """Parsing error."""


class ScriptRuntimeError(ScriptError):
    """Runtime error."""
