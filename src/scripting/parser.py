from __future__ import annotations

from .ast_nodes import (
    AssignExpr,
    BinaryExpr,
    Block,
    BreakStmt,
    CallExpr,
    ContinueStmt,
    ExprStmt,
    Expression,
    FunctionDecl,
    FunctionExpr,
    IdentifierExpr,
    IfStmt,
    LetStmt,
    ListExpr,
    LiteralExpr,
    Program,
    ReturnStmt,
    Statement,
    StringExpr,
    UnaryExpr,
    WhileStmt,
)
from .errors import ScriptError, ScriptParseError
from .lexer import Lexer, Token


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._index = 0

    @classmethod
    def parse_script(cls, source: str) -> Program:
        tokens = Lexer(source).tokenize()
        parser = cls(tokens)
        return parser.parse_program()

    @classmethod
    def parse_inline_expression(cls, source: str, *, line: int) -> Expression:
        try:
            tokens = Lexer(source).tokenize()
            parser = cls(tokens)
            expr = parser._parse_expression()
            if not parser._check("EOF"):
                token = parser._peek()
                raise ScriptParseError("Unexpected token in interpolation.", line=token.line)
            return expr
        except ScriptError as exc:
            raise ScriptParseError(exc.message, line=line) from exc

    def parse_program(self) -> Program:
        statements: list[Statement] = []
        self._consume_separators()
        while not self._check("EOF"):
            statements.append(self._parse_statement())
            self._consume_separators()
        return Program(statements=statements, line=1)

    def _parse_statement(self) -> Statement:
        if self._match("LET"):
            return self._parse_let()
        if self._check("FN") and self._check_next("IDENT"):
            fn_token = self._advance()
            return self._parse_function_decl(fn_token.line)
        if self._match("IF"):
            return self._parse_if(self._previous().line)
        if self._match("WHILE"):
            return self._parse_while(self._previous().line)
        if self._match("RETURN"):
            return self._parse_return(self._previous().line)
        if self._match("BREAK"):
            return BreakStmt(line=self._previous().line)
        if self._match("CONTINUE"):
            return ContinueStmt(line=self._previous().line)

        expr = self._parse_expression()
        return ExprStmt(expr=expr, line=expr.line)

    def _parse_let(self) -> LetStmt:
        token = self._expect("IDENT", "Expected variable name after let.")
        self._expect("EQ", "Expected '=' after variable name.")
        value = self._parse_expression()
        return LetStmt(name=str(token.value), value=value, line=token.line)

    def _parse_function_decl(self, line: int) -> FunctionDecl:
        name_token = self._expect("IDENT", "Expected function name.")
        params, body = self._parse_function_tail()
        return FunctionDecl(name=str(name_token.value), params=params, body=body, line=line)

    def _parse_if(self, line: int) -> IfStmt:
        condition = self._parse_expression()
        then_block = self._parse_block()
        else_block: Block | None = None
        if self._match("ELSE"):
            else_block = self._parse_block()
        return IfStmt(condition=condition, then_block=then_block, else_block=else_block, line=line)

    def _parse_while(self, line: int) -> WhileStmt:
        condition = self._parse_expression()
        body = self._parse_block()
        return WhileStmt(condition=condition, body=body, line=line)

    def _parse_return(self, line: int) -> ReturnStmt:
        if self._check("SEMICOLON") or self._check("NEWLINE") or self._check("RBRACE") or self._check("EOF"):
            return ReturnStmt(value=None, line=line)
        value = self._parse_expression()
        return ReturnStmt(value=value, line=line)

    def _parse_block(self) -> Block:
        brace = self._expect("LBRACE", "Expected '{' to start block.")
        statements: list[Statement] = []
        self._consume_separators()
        while not self._check("RBRACE"):
            if self._check("EOF"):
                raise ScriptParseError("Unterminated block.", line=brace.line)
            statements.append(self._parse_statement())
            self._consume_separators()
        self._expect("RBRACE", "Expected '}' to close block.")
        return Block(statements=statements, line=brace.line)

    def _parse_expression(self) -> Expression:
        return self._parse_assignment()

    def _parse_assignment(self) -> Expression:
        expr = self._parse_or()
        if self._match("EQ"):
            eq = self._previous()
            value = self._parse_assignment()
            if isinstance(expr, IdentifierExpr):
                return AssignExpr(name=expr.name, value=value, line=eq.line)
            raise ScriptParseError("Invalid assignment target.", line=eq.line)
        return expr

    def _parse_or(self) -> Expression:
        expr = self._parse_and()
        while self._match("OR"):
            op = self._previous()
            right = self._parse_and()
            expr = BinaryExpr(left=expr, operator=str(op.value), right=right, line=op.line)
        return expr

    def _parse_and(self) -> Expression:
        expr = self._parse_equality()
        while self._match("AND"):
            op = self._previous()
            right = self._parse_equality()
            expr = BinaryExpr(left=expr, operator=str(op.value), right=right, line=op.line)
        return expr

    def _parse_equality(self) -> Expression:
        expr = self._parse_comparison()
        while self._match("EQ_EQ", "BANG_EQ"):
            op = self._previous()
            right = self._parse_comparison()
            expr = BinaryExpr(left=expr, operator=str(op.value), right=right, line=op.line)
        return expr

    def _parse_comparison(self) -> Expression:
        expr = self._parse_term()
        while self._match("LT", "LT_EQ", "GT", "GT_EQ"):
            op = self._previous()
            right = self._parse_term()
            expr = BinaryExpr(left=expr, operator=str(op.value), right=right, line=op.line)
        return expr

    def _parse_term(self) -> Expression:
        expr = self._parse_factor()
        while self._match("PLUS", "MINUS"):
            op = self._previous()
            right = self._parse_factor()
            expr = BinaryExpr(left=expr, operator=str(op.value), right=right, line=op.line)
        return expr

    def _parse_factor(self) -> Expression:
        expr = self._parse_unary()
        while self._match("STAR", "SLASH", "PERCENT"):
            op = self._previous()
            right = self._parse_unary()
            expr = BinaryExpr(left=expr, operator=str(op.value), right=right, line=op.line)
        return expr

    def _parse_unary(self) -> Expression:
        if self._match("BANG", "MINUS"):
            op = self._previous()
            right = self._parse_unary()
            return UnaryExpr(operator=str(op.value), right=right, line=op.line)
        return self._parse_call()

    def _parse_call(self) -> Expression:
        expr = self._parse_primary()
        while True:
            if self._match("LPAREN"):
                line = self._previous().line
                args: list[Expression] = []
                if not self._check("RPAREN"):
                    args.append(self._parse_expression())
                    while self._match("COMMA"):
                        args.append(self._parse_expression())
                self._expect("RPAREN", "Expected ')' after arguments.")
                expr = CallExpr(callee=expr, args=args, line=line)
                continue
            break
        return expr

    def _parse_primary(self) -> Expression:
        if self._match("NUMBER"):
            token = self._previous()
            return LiteralExpr(value=token.value, line=token.line)

        if self._match("STRING"):
            token = self._previous()
            text, formatted = token.value
            return StringExpr(value=str(text), formatted=bool(formatted), line=token.line)

        if self._match("TRUE"):
            token = self._previous()
            return LiteralExpr(value=True, line=token.line)

        if self._match("FALSE"):
            token = self._previous()
            return LiteralExpr(value=False, line=token.line)

        if self._match("NULL"):
            token = self._previous()
            return LiteralExpr(value=None, line=token.line)

        if self._match("IDENT"):
            token = self._previous()
            return IdentifierExpr(name=str(token.value), line=token.line)

        if self._match("LPAREN"):
            expr = self._parse_expression()
            self._expect("RPAREN", "Expected ')' after expression.")
            return expr

        if self._match("LBRACKET"):
            line = self._previous().line
            items: list[Expression] = []
            if not self._check("RBRACKET"):
                items.append(self._parse_expression())
                while self._match("COMMA"):
                    items.append(self._parse_expression())
            self._expect("RBRACKET", "Expected ']' after list literal.")
            return ListExpr(items=items, line=line)

        if self._match("FN"):
            line = self._previous().line
            params, body = self._parse_function_tail()
            return FunctionExpr(params=params, body=body, line=line)

        token = self._peek()
        raise ScriptParseError(f"Unexpected token: {token.token_type}", line=token.line)

    def _parse_function_tail(self) -> tuple[list[str], Block]:
        self._expect("LPAREN", "Expected '(' after fn.")
        params: list[str] = []
        if not self._check("RPAREN"):
            token = self._expect("IDENT", "Expected parameter name.")
            params.append(str(token.value))
            while self._match("COMMA"):
                token = self._expect("IDENT", "Expected parameter name.")
                params.append(str(token.value))
        self._expect("RPAREN", "Expected ')' after parameter list.")
        body = self._parse_block()
        return params, body

    def _consume_separators(self) -> None:
        while self._match("SEMICOLON", "NEWLINE"):
            continue

    def _expect(self, token_type: str, message: str) -> Token:
        if self._check(token_type):
            return self._advance()
        raise ScriptParseError(message, line=self._peek().line)

    def _match(self, *token_types: str) -> bool:
        for token_type in token_types:
            if self._check(token_type):
                self._advance()
                return True
        return False

    def _check(self, token_type: str) -> bool:
        return self._peek().token_type == token_type

    def _check_next(self, token_type: str) -> bool:
        if self._index + 1 >= len(self._tokens):
            return False
        return self._tokens[self._index + 1].token_type == token_type

    def _advance(self) -> Token:
        if not self._is_at_end():
            self._index += 1
        return self._previous()

    def _peek(self) -> Token:
        return self._tokens[self._index]

    def _previous(self) -> Token:
        return self._tokens[self._index - 1]

    def _is_at_end(self) -> bool:
        return self._peek().token_type == "EOF"
