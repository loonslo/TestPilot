"""A tiny, safe expression language for the checks of a case.

Arithmetic (``+ - *``), comparisons, a few functions and ``{{step.path}}`` references to earlier
answers, nothing else. It is parsed by hand and evaluated over data; text is never handed to
``eval`` or any interpreter. Example:
``{{s3.data.base.income_minor}} - {{s2.data.base.income_minor}} == -10000``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<ref>\{\{\s*[a-z][a-z0-9_]{0,23}\.[A-Za-z0-9_.\-]+\s*\}\})
      | (?P<num>\d+(?:\.\d+)?)
      | (?P<str>"[^"\\]*"|'[^'\\]*')
      | (?P<op>==|!=|<=|>=|<|>|\+|-|\*|\(|\)|,)
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)
_REF_PARTS = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,23})\.([A-Za-z0-9_.\-]+)\s*\}\}")
_COMPARISONS = {"==", "!=", "<=", ">=", "<", ">"}
_FUNCTIONS = {"len", "abs", "sum"}
_LITERALS = {"true": True, "false": False, "null": None}
MAX_LENGTH = 400

Node = tuple[Any, ...]


class ExprError(ValueError):
    """The expression is malformed or cannot be evaluated."""


class MissingValue(ExprError):
    """A reference points at something the answer does not contain."""


def _tokens(text: str) -> list[tuple[str, str]]:
    position, found = 0, []
    while position < len(text):
        if not text[position:].strip():
            break
        match = _TOKEN.match(text, position)
        if not match or match.end() == position:
            raise ExprError(f"unexpected text at: {text[position : position + 20]!r}")
        found.append((match.lastgroup or "", match.group(match.lastgroup or "").strip()))
        position = match.end()
    return found


class _Parser:
    def __init__(self, text: str) -> None:
        if len(text) > MAX_LENGTH:
            raise ExprError(f"an expression may have at most {MAX_LENGTH} characters")
        self.tokens = _tokens(text)
        self.index = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise ExprError("the expression ends too early")
        self.index += 1
        return token

    def expect(self, value: str) -> None:
        kind, text = self.take()
        if text != value or kind != "op":
            raise ExprError(f"expected {value!r}, found {text!r}")

    def parse(self) -> Node:
        node = self.comparison()
        if self.peek() is not None:
            raise ExprError(f"unexpected {self.peek()[1]!r} after a complete expression")  # type: ignore[index]
        return node

    def comparison(self) -> Node:
        left = self.sum()
        token = self.peek()
        if token and token[0] == "op" and token[1] in _COMPARISONS:
            self.take()
            return ("cmp", token[1], left, self.sum())
        return left

    def sum(self) -> Node:
        node = self.term()
        while (token := self.peek()) and token[0] == "op" and token[1] in {"+", "-"}:
            self.take()
            node = ("bin", token[1], node, self.term())
        return node

    def term(self) -> Node:
        node = self.unary()
        while (token := self.peek()) and token[0] == "op" and token[1] == "*":
            self.take()
            node = ("bin", "*", node, self.unary())
        return node

    def unary(self) -> Node:
        token = self.peek()
        if token and token[0] == "op" and token[1] == "-":
            self.take()
            return ("neg", self.unary())
        return self.primary()

    def primary(self) -> Node:
        kind, text = self.take()
        if kind == "num":
            return ("num", Decimal(text))
        if kind == "str":
            return ("str", text[1:-1])
        if kind == "ref":
            match = _REF_PARTS.fullmatch(text)
            assert match is not None
            return ("ref", match.group(1), match.group(2))
        if kind == "op" and text == "(":
            node = self.comparison()
            self.expect(")")
            return node
        if kind == "name":
            if text in _LITERALS:
                return ("lit", _LITERALS[text])
            if text in _FUNCTIONS:
                self.expect("(")
                args: list[Node] = []
                if not (self.peek() and self.peek()[1] == ")"):  # type: ignore[index]
                    args.append(self.comparison())
                    while self.peek() and self.peek()[1] == ",":  # type: ignore[index]
                        self.take()
                        args.append(self.comparison())
                self.expect(")")
                return ("call", text, args)
            raise ExprError(f"unknown name {text!r}; use true, false, null, len, abs or sum")
        raise ExprError(f"unexpected {text!r}")


def parse(text: str) -> Node:
    return _Parser(text).parse()


def references(node: Node) -> set[str]:
    """The step ids an expression refers to."""
    if node[0] == "ref":
        return {node[1]}
    found: set[str] = set()
    for part in node[1:]:
        if isinstance(part, tuple):
            found |= references(part)
        elif isinstance(part, list):
            for item in part:
                found |= references(item)
    return found


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        raise ExprError(f"{value!r} is not a number")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ExprError(f"{value!r} is not a number") from exc


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def evaluate_node(node: Node, lookup: Callable[[str, str], Any]) -> Any:
    kind = node[0]
    if kind in {"num", "str", "lit"}:
        return node[1]
    if kind == "ref":
        return lookup(node[1], node[2])
    if kind == "neg":
        return -_number(evaluate_node(node[1], lookup))
    if kind == "bin":
        left = _number(evaluate_node(node[2], lookup))
        right = _number(evaluate_node(node[3], lookup))
        return {"+": left + right, "-": left - right, "*": left * right}[node[1]]
    if kind == "cmp":
        left, right = evaluate_node(node[2], lookup), evaluate_node(node[3], lookup)
        if _is_number(left) and _is_number(right):
            left, right = Decimal(str(left)), Decimal(str(right))
        elif node[1] not in {"==", "!="}:
            raise ExprError(f"cannot order {left!r} and {right!r}")
        operator = node[1]
        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator == ">":
            return left > right
        return left >= right
    if kind == "call":
        return _call(node[1], [evaluate_node(arg, lookup) for arg in node[2]])
    raise ExprError(f"cannot evaluate {kind}")


def _call(name: str, args: list[Any]) -> Any:
    if name == "len":
        if len(args) != 1 or not isinstance(args[0], list | str | dict):
            raise ExprError("len takes one list, string or object")
        return Decimal(len(args[0]))
    if name == "abs":
        if len(args) != 1:
            raise ExprError("abs takes one number")
        return abs(_number(args[0]))
    if name == "sum":
        if len(args) not in (2, 4) or not isinstance(args[0], list) or not isinstance(args[1], str):
            raise ExprError('sum takes (list, "field") or (list, "field", "where_field", value)')
        total = Decimal(0)
        for item in args[0]:
            if not isinstance(item, dict):
                continue
            if len(args) == 4 and item.get(args[2]) != args[3]:
                continue
            total += _number(item.get(args[1]))
        return total
    raise ExprError(f"unknown function {name!r}")


def evaluate(text: str, lookup: Callable[[str, str], Any]) -> Any:
    return evaluate_node(parse(text), lookup)
