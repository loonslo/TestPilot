from decimal import Decimal
from typing import Any

import pytest

from api_test_agent import expr

DATA: dict[str, dict[str, Any]] = {
    "s1": {
        "status": 200,
        "data": {
            "base": {"income_minor": 12500, "expense_minor": 5300, "name": "cny"},
            "items": [
                {"currency": "CNY", "value": 100},
                {"currency": "USD", "value": 30},
                {"currency": "CNY", "value": 5},
            ],
        },
    },
    "s2": {"status": 409, "error": {"code": "VERSION_CONFLICT"}},
}


def look(step: str, path: str) -> Any:
    current: Any = DATA[step]
    for part in path.split("."):
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, ValueError) as exc:
            raise expr.MissingValue(f"{step}.{path} is missing") from exc
    return current


def run(text: str) -> Any:
    return expr.evaluate(text, look)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 + 2 * 3 == 7", True),
        ("(1 + 2) * 3 == 9", True),
        ("10 - 2 - 3 == 5", True),
        ("-5 + 8 == 3", True),
        ("0.1 + 0.2 == 0.3", True),  # decimal arithmetic, not binary floats
        ("{{s1.data.base.income_minor}} - {{s1.data.base.expense_minor}} == 7200", True),
        ("{{s1.data.base.income_minor}} > {{s1.data.base.expense_minor}}", True),
        ("{{s1.status}} == 200", True),
        ("{{s2.error.code}} == 'VERSION_CONFLICT'", True),
        ('{{s2.error.code}} != "NOT_FOUND"', True),
        ("{{s1.data.base.name}} == 'cny'", True),
        ("true == true", True),
        ("null == null", True),
        ("len({{s1.data.items}}) == 3", True),
        ("abs(-4) == 4", True),
        ("abs(0 - {{s1.data.base.expense_minor}}) == 5300", True),
        ('sum({{s1.data.items}}, "value") == 135', True),
        ('sum({{s1.data.items}}, "value", "currency", "CNY") == 105', True),
        ("{{s1.data.items.1.value}} == 30", True),
        ("1 > 2", False),
        ("{{s1.status}} != 200", False),
    ],
)
def test_expressions_evaluate(text: str, expected: bool) -> None:
    assert run(text) is expected


def test_arithmetic_returns_exact_decimals() -> None:
    assert run("0.1 + 0.2") == Decimal("0.3")
    assert run("3 * 4") == Decimal(12)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1 +",
        "1 2",
        "(1 + 2",
        "1 == 2 == 3",
        "unknown_name",
        "__import__('os')",
        "len",
        "{{s1.data}}.__class__",
        "1; 2",
        "a.b",
        "'unterminated",
        "1 === 2",
    ],
)
def test_malformed_expressions_are_rejected_at_parse_time(text: str) -> None:
    with pytest.raises(expr.ExprError):
        expr.parse(text)


def test_no_function_outside_the_whitelist_can_be_called() -> None:
    for text in ("open('x')", "eval('1')", "print(1)", "str(1)", "int(1)"):
        with pytest.raises(expr.ExprError):
            expr.parse(text)


def test_an_overlong_expression_is_refused() -> None:
    with pytest.raises(expr.ExprError, match="at most"):
        expr.parse("1 + " * 200 + "1")


@pytest.mark.parametrize(
    "text",
    ["'a' < 'b'", "true + 1", "{{s1.data.base.name}} * 2", "len(5) == 1", "abs('x') == 1"],
)
def test_wrong_kinds_of_values_are_errors(text: str) -> None:
    with pytest.raises(expr.ExprError):
        run(text)


def test_a_missing_value_is_a_distinct_error() -> None:
    with pytest.raises(expr.MissingValue):
        run("{{s1.data.nope}} == 1")
    with pytest.raises(expr.MissingValue):
        run("{{s1.data.items.9.value}} == 1")


def test_references_lists_the_steps_an_expression_uses() -> None:
    node = expr.parse("{{s3.data.a}} - {{s2.data.b}} == sum({{s1.data.items}}, 'value')")
    assert expr.references(node) == {"s1", "s2", "s3"}
    assert expr.references(expr.parse("1 + 2 == 3")) == set()


def test_ordering_comparisons_work_across_ints_floats_and_decimals() -> None:
    assert run("2 < 2.5") is True
    assert run("{{s1.data.base.income_minor}} >= 12500") is True
    assert run("{{s1.data.base.income_minor}} <= 12499") is False
