from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

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
from .errors import ScriptError, ScriptRuntimeError
from .parser import Parser


class Environment:
    def __init__(self, parent: Environment | None = None) -> None:
        self.parent = parent
        self.values: dict[str, Any] = {}

    def define(self, name: str, value: Any) -> None:
        self.values[name] = value

    def get(self, name: str, *, line: int) -> Any:
        if name in self.values:
            return self.values[name]
        if self.parent is not None:
            return self.parent.get(name, line=line)
        raise ScriptRuntimeError(f"Undefined variable: {name}", line=line)

    def assign(self, name: str, value: Any, *, line: int) -> None:
        if name in self.values:
            self.values[name] = value
            return
        if self.parent is not None:
            self.parent.assign(name, value, line=line)
            return
        raise ScriptRuntimeError(f"Undefined variable: {name}", line=line)


class _ReturnSignal(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__()
        self.value = value


class _BreakSignal(Exception):
    pass


class _ContinueSignal(Exception):
    pass


class ScriptCallable:
    def call(self, interpreter: ScriptInterpreter, args: list[Any], *, line: int) -> Any:
        raise NotImplementedError


@dataclass(slots=True)
class NativeFunction(ScriptCallable):
    name: str
    callback: Callable[[list[Any], int], Any]

    def call(self, interpreter: ScriptInterpreter, args: list[Any], *, line: int) -> Any:
        try:
            return self.callback(args, line)
        except ScriptRuntimeError:
            raise
        except Exception as exc:
            raise ScriptRuntimeError(f"Native function '{self.name}' failed: {exc}", line=line) from exc


@dataclass(slots=True)
class ScriptFunction(ScriptCallable):
    params: list[str]
    body: Block
    closure: Environment
    name: str | None = None

    def call(self, interpreter: ScriptInterpreter, args: list[Any], *, line: int) -> Any:
        if len(args) != len(self.params):
            name = self.name or "<anonymous>"
            raise ScriptRuntimeError(
                f"Function '{name}' expects {len(self.params)} arg(s), got {len(args)}.",
                line=line,
            )
        local = Environment(parent=self.closure)
        for key, value in zip(self.params, args):
            local.define(key, value)
        try:
            interpreter._execute_statements(self.body.statements, local)
        except _ReturnSignal as signal:
            return signal.value
        return None


class ScriptInterpreter:
    def __init__(self, *, step_limit: int = 100_000) -> None:
        self.step_limit = max(1000, int(step_limit))
        self._steps = 0
        self._builtin_env = Environment()
        self._register_standard_natives()

    def create_global_env(self) -> Environment:
        return Environment(parent=self._builtin_env)

    def register_native(self, name: str, callback: Callable[[list[Any], int], Any]) -> None:
        self._builtin_env.define(name, NativeFunction(name=name, callback=callback))

    def execute(self, program: Program, env: Environment) -> None:
        self._steps = 0
        self._execute_statements(program.statements, env)

    def call_function(self, value: Any, args: list[Any], *, line: int) -> Any:
        self._steps = 0
        return self._call_callable(value, args, line=line)

    def _execute_statements(self, statements: list[Statement], env: Environment) -> None:
        for statement in statements:
            self._execute_statement(statement, env)

    def _execute_statement(self, statement: Statement, env: Environment) -> None:
        self._tick(statement.line)

        if isinstance(statement, LetStmt):
            env.define(statement.name, self._evaluate(statement.value, env))
            return

        if isinstance(statement, ExprStmt):
            self._evaluate(statement.expr, env)
            return

        if isinstance(statement, FunctionDecl):
            function = ScriptFunction(
                params=statement.params,
                body=statement.body,
                closure=env,
                name=statement.name,
            )
            env.define(statement.name, function)
            return

        if isinstance(statement, IfStmt):
            condition = self._evaluate(statement.condition, env)
            if self._is_truthy(condition):
                self._execute_block(statement.then_block, env)
            elif statement.else_block is not None:
                self._execute_block(statement.else_block, env)
            return

        if isinstance(statement, WhileStmt):
            while self._is_truthy(self._evaluate(statement.condition, env)):
                self._tick(statement.line)
                try:
                    self._execute_block(statement.body, env)
                except _ContinueSignal:
                    continue
                except _BreakSignal:
                    break
            return

        if isinstance(statement, ReturnStmt):
            value = self._evaluate(statement.value, env) if statement.value is not None else None
            raise _ReturnSignal(value)

        if isinstance(statement, BreakStmt):
            raise _BreakSignal()

        if isinstance(statement, ContinueStmt):
            raise _ContinueSignal()

        raise ScriptRuntimeError("Unsupported statement.", line=statement.line)

    def _execute_block(self, block: Block, env: Environment) -> None:
        local = Environment(parent=env)
        self._execute_statements(block.statements, local)

    def _evaluate(self, expression: Expression | None, env: Environment) -> Any:
        if expression is None:
            return None

        self._tick(expression.line)

        if isinstance(expression, LiteralExpr):
            return expression.value

        if isinstance(expression, StringExpr):
            if expression.formatted:
                return self._format_string(expression.value, env, line=expression.line)
            return expression.value

        if isinstance(expression, IdentifierExpr):
            return env.get(expression.name, line=expression.line)

        if isinstance(expression, AssignExpr):
            value = self._evaluate(expression.value, env)
            env.assign(expression.name, value, line=expression.line)
            return value

        if isinstance(expression, UnaryExpr):
            right = self._evaluate(expression.right, env)
            if expression.operator == "-":
                return -self._to_number(right, line=expression.line)
            if expression.operator == "!":
                return not self._is_truthy(right)
            raise ScriptRuntimeError(f"Unsupported unary operator: {expression.operator}", line=expression.line)

        if isinstance(expression, BinaryExpr):
            op = expression.operator
            if op == "and":
                left = self._evaluate(expression.left, env)
                return self._evaluate(expression.right, env) if self._is_truthy(left) else left
            if op == "or":
                left = self._evaluate(expression.left, env)
                return left if self._is_truthy(left) else self._evaluate(expression.right, env)

            left = self._evaluate(expression.left, env)
            right = self._evaluate(expression.right, env)
            if op == "+":
                if isinstance(left, str) or isinstance(right, str):
                    return f"{self._to_string(left)}{self._to_string(right)}"
                return self._to_number(left, line=expression.line) + self._to_number(right, line=expression.line)
            if op == "-":
                return self._to_number(left, line=expression.line) - self._to_number(right, line=expression.line)
            if op == "*":
                return self._to_number(left, line=expression.line) * self._to_number(right, line=expression.line)
            if op == "/":
                right_value = self._to_number(right, line=expression.line)
                if right_value == 0:
                    raise ScriptRuntimeError("Division by zero.", line=expression.line)
                return self._to_number(left, line=expression.line) / right_value
            if op == "%":
                right_value = self._to_number(right, line=expression.line)
                if right_value == 0:
                    raise ScriptRuntimeError("Modulo by zero.", line=expression.line)
                return self._to_number(left, line=expression.line) % right_value
            if op == "==":
                return left == right
            if op == "!=":
                return left != right
            if op == "<":
                return self._to_number(left, line=expression.line) < self._to_number(right, line=expression.line)
            if op == "<=":
                return self._to_number(left, line=expression.line) <= self._to_number(right, line=expression.line)
            if op == ">":
                return self._to_number(left, line=expression.line) > self._to_number(right, line=expression.line)
            if op == ">=":
                return self._to_number(left, line=expression.line) >= self._to_number(right, line=expression.line)
            raise ScriptRuntimeError(f"Unsupported binary operator: {op}", line=expression.line)

        if isinstance(expression, CallExpr):
            callee = self._evaluate(expression.callee, env)
            args = [self._evaluate(item, env) for item in expression.args]
            return self._call_callable(callee, args, line=expression.line)

        if isinstance(expression, FunctionExpr):
            return ScriptFunction(params=expression.params, body=expression.body, closure=env, name=None)

        if isinstance(expression, ListExpr):
            return [self._evaluate(item, env) for item in expression.items]

        raise ScriptRuntimeError("Unsupported expression.", line=expression.line)

    def _call_callable(self, callee: Any, args: list[Any], *, line: int) -> Any:
        if not isinstance(callee, ScriptCallable):
            raise ScriptRuntimeError("Attempted to call a non-function value.", line=line)
        return callee.call(self, args, line=line)

    def _format_string(self, template: str, env: Environment, *, line: int) -> str:
        result: list[str] = []
        index = 0
        while index < len(template):
            if template.startswith("{{", index):
                result.append("{")
                index += 2
                continue
            if template.startswith("}}", index):
                result.append("}")
                index += 2
                continue
            if template[index] == "{":
                end = template.find("}", index + 1)
                if end < 0:
                    raise ScriptRuntimeError("Unterminated interpolation block.", line=line)
                inner = template[index + 1 : end].strip()
                if not inner:
                    raise ScriptRuntimeError("Empty interpolation block.", line=line)
                try:
                    expr = Parser.parse_inline_expression(inner, line=line)
                except ScriptError as exc:
                    raise ScriptRuntimeError(f"Interpolation parse failed: {exc.message}", line=line) from exc
                value = self._evaluate(expr, env)
                result.append(self._to_string(value))
                index = end + 1
                continue
            result.append(template[index])
            index += 1
        return "".join(result)

    def _to_number(self, value: Any, *, line: int) -> float:
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
        raise ScriptRuntimeError(f"Expected numeric value, got {type(value).__name__}.", line=line)

    def _to_string(self, value: Any) -> str:
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, list):
            return "[" + ", ".join(self._to_string(item) for item in value) + "]"
        return str(value)

    def _is_truthy(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value != ""
        if isinstance(value, list):
            return len(value) > 0
        return True

    def _tick(self, line: int) -> None:
        self._steps += 1
        if self._steps > self.step_limit:
            raise ScriptRuntimeError(
                f"Step limit exceeded ({self.step_limit}). Potential infinite loop detected.",
                line=line,
            )

    def _register_standard_natives(self) -> None:
        self.register_native("len", self._native_len)
        self.register_native("str", self._native_str)
        self.register_native("int", self._native_int)
        self.register_native("float", self._native_float)
        self.register_native("split", self._native_split)
        self.register_native("join", self._native_join)
        self.register_native("sort", self._native_sort)
        self.register_native("upper", self._native_upper)
        self.register_native("lower", self._native_lower)
        self.register_native("replace", self._native_replace)
        self.register_native("contains", self._native_contains)
        self.register_native("starts_with", self._native_starts_with)
        self.register_native("ends_with", self._native_ends_with)
        self.register_native("range", self._native_range)
        self.register_native("type_of", self._native_type_of)

    def _native_len(self, args: list[Any], line: int) -> int:
        self._expect_arity(args, 1, line=line, name="len")
        value = args[0]
        if isinstance(value, (str, list)):
            return len(value)
        raise ScriptRuntimeError("len() expects string or list.", line=line)

    def _native_str(self, args: list[Any], line: int) -> str:
        self._expect_arity(args, 1, line=line, name="str")
        return self._to_string(args[0])

    def _native_int(self, args: list[Any], line: int) -> int:
        self._expect_arity(args, 1, line=line, name="int")
        value = args[0]
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError as exc:
                raise ScriptRuntimeError(f"int() conversion failed: {value}", line=line) from exc
        raise ScriptRuntimeError("int() expects bool/number/string.", line=line)

    def _native_float(self, args: list[Any], line: int) -> float:
        self._expect_arity(args, 1, line=line, name="float")
        value = args[0]
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError as exc:
                raise ScriptRuntimeError(f"float() conversion failed: {value}", line=line) from exc
        raise ScriptRuntimeError("float() expects bool/number/string.", line=line)

    def _native_split(self, args: list[Any], line: int) -> list[str]:
        if len(args) not in {1, 2}:
            raise ScriptRuntimeError("split() expects 1 or 2 arguments.", line=line)
        text = self._expect_string(args[0], line=line, name="split")
        sep = self._expect_string(args[1], line=line, name="split") if len(args) == 2 else " "
        return text.split(sep)

    def _native_join(self, args: list[Any], line: int) -> str:
        self._expect_arity(args, 2, line=line, name="join")
        sep = self._expect_string(args[0], line=line, name="join")
        values = args[1]
        if not isinstance(values, list):
            raise ScriptRuntimeError("join() second argument must be a list.", line=line)
        return sep.join(self._to_string(item) for item in values)

    def _native_sort(self, args: list[Any], line: int) -> list[Any]:
        self._expect_arity(args, 1, line=line, name="sort")
        values = args[0]
        if not isinstance(values, list):
            raise ScriptRuntimeError("sort() expects a list.", line=line)
        return sorted(values)

    def _native_upper(self, args: list[Any], line: int) -> str:
        self._expect_arity(args, 1, line=line, name="upper")
        return self._expect_string(args[0], line=line, name="upper").upper()

    def _native_lower(self, args: list[Any], line: int) -> str:
        self._expect_arity(args, 1, line=line, name="lower")
        return self._expect_string(args[0], line=line, name="lower").lower()

    def _native_replace(self, args: list[Any], line: int) -> str:
        self._expect_arity(args, 3, line=line, name="replace")
        text = self._expect_string(args[0], line=line, name="replace")
        old = self._expect_string(args[1], line=line, name="replace")
        new = self._expect_string(args[2], line=line, name="replace")
        return text.replace(old, new)

    def _native_contains(self, args: list[Any], line: int) -> bool:
        self._expect_arity(args, 2, line=line, name="contains")
        text = self._expect_string(args[0], line=line, name="contains")
        sub = self._expect_string(args[1], line=line, name="contains")
        return sub in text

    def _native_starts_with(self, args: list[Any], line: int) -> bool:
        self._expect_arity(args, 2, line=line, name="starts_with")
        text = self._expect_string(args[0], line=line, name="starts_with")
        prefix = self._expect_string(args[1], line=line, name="starts_with")
        return text.startswith(prefix)

    def _native_ends_with(self, args: list[Any], line: int) -> bool:
        self._expect_arity(args, 2, line=line, name="ends_with")
        text = self._expect_string(args[0], line=line, name="ends_with")
        suffix = self._expect_string(args[1], line=line, name="ends_with")
        return text.endswith(suffix)

    def _native_range(self, args: list[Any], line: int) -> list[int]:
        if len(args) not in {1, 2, 3}:
            raise ScriptRuntimeError("range() expects 1, 2, or 3 arguments.", line=line)
        numbers = [int(self._to_number(arg, line=line)) for arg in args]
        return list(range(*numbers))

    def _native_type_of(self, args: list[Any], line: int) -> str:
        self._expect_arity(args, 1, line=line, name="type_of")
        value = args[0]
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "list"
        if isinstance(value, ScriptFunction):
            return "function"
        if isinstance(value, NativeFunction):
            return "native"
        return "object"

    def _expect_arity(self, args: list[Any], expected: int, *, line: int, name: str) -> None:
        if len(args) != expected:
            raise ScriptRuntimeError(f"{name}() expects {expected} argument(s).", line=line)

    def _expect_string(self, value: Any, *, line: int, name: str) -> str:
        if not isinstance(value, str):
            raise ScriptRuntimeError(f"{name}() expects string argument.", line=line)
        return value
