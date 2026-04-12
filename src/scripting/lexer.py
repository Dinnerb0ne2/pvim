from __future__ import annotations

from dataclasses import dataclass

from .errors import ScriptLexError

KEYWORDS = {
    "let": "LET",
    "fn": "FN",
    "if": "IF",
    "else": "ELSE",
    "while": "WHILE",
    "return": "RETURN",
    "break": "BREAK",
    "continue": "CONTINUE",
    "true": "TRUE",
    "false": "FALSE",
    "null": "NULL",
    "and": "AND",
    "or": "OR",
}

DOUBLE_TOKENS = {
    "==": "EQ_EQ",
    "!=": "BANG_EQ",
    "<=": "LT_EQ",
    ">=": "GT_EQ",
}

SINGLE_TOKENS = {
    "(": "LPAREN",
    ")": "RPAREN",
    "{": "LBRACE",
    "}": "RBRACE",
    "[": "LBRACKET",
    "]": "RBRACKET",
    ",": "COMMA",
    ";": "SEMICOLON",
    "+": "PLUS",
    "-": "MINUS",
    "*": "STAR",
    "/": "SLASH",
    "%": "PERCENT",
    "!": "BANG",
    "=": "EQ",
    "<": "LT",
    ">": "GT",
}


@dataclass(frozen=True, slots=True)
class Token:
    token_type: str
    value: object
    line: int


class Lexer:
    def __init__(self, source: str) -> None:
        self._source = source
        self._index = 0
        self._line = 1

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while not self._is_at_end():
            char = self._peek()

            if char in " \t\r":
                self._advance()
                continue

            if char == "\n":
                self._advance()
                tokens.append(Token("NEWLINE", "\n", self._line))
                self._line += 1
                continue

            if char == "#":
                self._skip_comment()
                continue

            if char == "f" and self._peek_next() in {'"', "'"}:
                line = self._line
                self._advance()
                text = self._read_string(self._advance(), line)
                tokens.append(Token("STRING", (text, True), line))
                continue

            if char in {'"', "'"}:
                line = self._line
                text = self._read_string(self._advance(), line)
                tokens.append(Token("STRING", (text, False), line))
                continue

            if char.isdigit():
                tokens.append(self._read_number())
                continue

            if char.isalpha() or char == "_":
                tokens.append(self._read_identifier())
                continue

            pair = f"{char}{self._peek_next()}"
            if pair in DOUBLE_TOKENS:
                line = self._line
                self._advance()
                self._advance()
                tokens.append(Token(DOUBLE_TOKENS[pair], pair, line))
                continue

            if char in SINGLE_TOKENS:
                line = self._line
                self._advance()
                tokens.append(Token(SINGLE_TOKENS[char], char, line))
                continue

            raise ScriptLexError(f"Unexpected character: {char}", line=self._line)

        tokens.append(Token("EOF", "", self._line))
        return tokens

    def _read_number(self) -> Token:
        line = self._line
        start = self._index
        while self._peek().isdigit():
            self._advance()

        is_float = False
        if self._peek() == "." and self._peek_next().isdigit():
            is_float = True
            self._advance()
            while self._peek().isdigit():
                self._advance()

        text = self._source[start : self._index]
        value: object = float(text) if is_float else int(text)
        return Token("NUMBER", value, line)

    def _read_identifier(self) -> Token:
        line = self._line
        start = self._index
        while self._peek().isalnum() or self._peek() == "_":
            self._advance()
        text = self._source[start : self._index]
        token_type = KEYWORDS.get(text, "IDENT")
        return Token(token_type, text, line)

    def _read_string(self, quote: str, line: int) -> str:
        chars: list[str] = []
        escaped = False
        while not self._is_at_end():
            char = self._advance()
            if escaped:
                chars.append(self._escaped(char))
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote:
                return "".join(chars)
            if char == "\n":
                self._line += 1
            chars.append(char)
        raise ScriptLexError("Unterminated string literal.", line=line)

    def _escaped(self, char: str) -> str:
        if char == "n":
            return "\n"
        if char == "t":
            return "\t"
        if char == "r":
            return "\r"
        if char in {'"', "'", "\\", "{", "}"}:
            return char
        return char

    def _skip_comment(self) -> None:
        while not self._is_at_end() and self._peek() != "\n":
            self._advance()

    def _peek(self) -> str:
        if self._is_at_end():
            return "\0"
        return self._source[self._index]

    def _peek_next(self) -> str:
        if self._index + 1 >= len(self._source):
            return "\0"
        return self._source[self._index + 1]

    def _advance(self) -> str:
        char = self._source[self._index]
        self._index += 1
        return char

    def _is_at_end(self) -> bool:
        return self._index >= len(self._source)
