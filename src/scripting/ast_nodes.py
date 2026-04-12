from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Program:
    statements: list[Statement]
    line: int


@dataclass(frozen=True, slots=True)
class Block:
    statements: list[Statement]
    line: int


class Statement:
    line: int


class Expression:
    line: int


@dataclass(frozen=True, slots=True)
class LetStmt(Statement):
    name: str
    value: Expression
    line: int


@dataclass(frozen=True, slots=True)
class ExprStmt(Statement):
    expr: Expression
    line: int


@dataclass(frozen=True, slots=True)
class IfStmt(Statement):
    condition: Expression
    then_block: Block
    else_block: Block | None
    line: int


@dataclass(frozen=True, slots=True)
class WhileStmt(Statement):
    condition: Expression
    body: Block
    line: int


@dataclass(frozen=True, slots=True)
class ReturnStmt(Statement):
    value: Expression | None
    line: int


@dataclass(frozen=True, slots=True)
class BreakStmt(Statement):
    line: int


@dataclass(frozen=True, slots=True)
class ContinueStmt(Statement):
    line: int


@dataclass(frozen=True, slots=True)
class FunctionDecl(Statement):
    name: str
    params: list[str]
    body: Block
    line: int


@dataclass(frozen=True, slots=True)
class LiteralExpr(Expression):
    value: Any
    line: int


@dataclass(frozen=True, slots=True)
class StringExpr(Expression):
    value: str
    formatted: bool
    line: int


@dataclass(frozen=True, slots=True)
class IdentifierExpr(Expression):
    name: str
    line: int


@dataclass(frozen=True, slots=True)
class AssignExpr(Expression):
    name: str
    value: Expression
    line: int


@dataclass(frozen=True, slots=True)
class UnaryExpr(Expression):
    operator: str
    right: Expression
    line: int


@dataclass(frozen=True, slots=True)
class BinaryExpr(Expression):
    left: Expression
    operator: str
    right: Expression
    line: int


@dataclass(frozen=True, slots=True)
class CallExpr(Expression):
    callee: Expression
    args: list[Expression]
    line: int


@dataclass(frozen=True, slots=True)
class FunctionExpr(Expression):
    params: list[str]
    body: Block
    line: int


@dataclass(frozen=True, slots=True)
class ListExpr(Expression):
    items: list[Expression]
    line: int
