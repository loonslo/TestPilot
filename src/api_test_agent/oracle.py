"""Independent arithmetic for the ledger rules (LED-01 to LED-04 in benchmarks/public/rules.json).

Pure functions, no I/O and no import from the target. They recompute what ``ledger.summary`` must
report from the records that the list endpoints return, using only the rule statements. A mismatch
means either the service or the rule is wrong; either way a human looks at it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

INCOME_TYPES = frozenset({"deposit", "dividend", "interest"})
EXPENSE_TYPES = frozenset({"withdrawal", "fee"})
AGGREGATE_KEYS = (
    "cash_balance_minor",
    "holdings_value_minor",
    "holdings_cost_minor",
    "unrealized_profit_minor",
    "total_assets_minor",
    "income_minor",
    "expense_minor",
    "net_cashflow_minor",
)
_COMPARED = ("month", "base_currency", "base", "by_currency", "excluded_currencies")


def to_minor(value: Any) -> int:
    """Amount in currency units (number or decimal string) to integer minor units, half-up."""
    return int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Valuation:
    market_value_minor: int
    cost_minor: int
    unrealized_profit_minor: int | None


def value_holding(holding: Mapping[str, Any]) -> Valuation:
    """LED-03: value = quantity x price, falling back to the average cost; cash costs 1 a unit."""
    quantity = Decimal(str(holding["quantity"]))
    is_cash = holding["asset_type"] == "cash"
    average_cost = holding.get("average_cost")
    price = holding.get("current_price")
    cost_unit = Decimal(str(average_cost)) if average_cost is not None else Decimal(int(is_cash))
    price_unit = Decimal(str(price)) if price is not None else cost_unit
    value = to_minor(quantity * price_unit)
    cost = to_minor(quantity * cost_unit)
    profit = value - cost if average_cost is not None else None
    return Valuation(value, cost, profit)


def expected_summary(
    holdings: Iterable[Mapping[str, Any]],
    transactions: Iterable[Mapping[str, Any]],
    month: str,
    base_currency: str,
) -> dict[str, Any]:
    """LED-01 to LED-04: per-currency totals; only the base currency feeds ``base``."""
    totals: dict[str, dict[str, int]] = {}

    def bucket(currency: str) -> dict[str, int]:
        return totals.setdefault(currency, dict.fromkeys(AGGREGATE_KEYS, 0))

    bucket(base_currency)
    for holding in holdings:
        valuation = value_holding(holding)
        current = bucket(holding["currency"])
        if holding["asset_type"] == "cash":
            current["cash_balance_minor"] += valuation.market_value_minor
        else:
            current["holdings_value_minor"] += valuation.market_value_minor
            current["holdings_cost_minor"] += valuation.cost_minor
            current["unrealized_profit_minor"] += valuation.unrealized_profit_minor or 0
        current["total_assets_minor"] += valuation.market_value_minor
    for transaction in transactions:
        if not str(transaction["transaction_date"]).startswith(month):
            continue
        current = bucket(transaction["currency"])
        amount = abs(int(transaction["amount_minor"]))
        kind = transaction["transaction_type"]
        if kind in INCOME_TYPES:
            current["income_minor"] += amount
        elif kind in EXPENSE_TYPES:
            current["expense_minor"] += amount
    for current in totals.values():
        current["net_cashflow_minor"] = current["income_minor"] - current["expense_minor"]
    return {
        "month": month,
        "base_currency": base_currency,
        "base": dict(totals[base_currency]),
        "by_currency": totals,
        "excluded_currencies": sorted(c for c in totals if c != base_currency),
    }


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def compare_summary(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """Differences between the expected and the reported summary (empty list means equal)."""
    left = _flatten({key: expected[key] for key in _COMPARED})
    right = _flatten({key: actual.get(key) for key in _COMPARED})
    return [
        f"{path}: expected {left.get(path)!r}, got {right.get(path)!r}"
        for path in sorted(left.keys() | right.keys())
        if left.get(path) != right.get(path)
    ]
