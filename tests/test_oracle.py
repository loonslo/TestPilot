import copy
import json
from pathlib import Path

import pytest

from api_test_agent.oracle import (
    AGGREGATE_KEYS,
    compare_summary,
    expected_summary,
    to_minor,
    value_holding,
)

FIXTURE = Path(__file__).parent / "fixtures" / "reference_case_oct.json"


@pytest.fixture(scope="module")
def recorded() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def holding(**fields) -> dict:
    base = {
        "asset_type": "fund",
        "quantity": 1,
        "average_cost": None,
        "current_price": None,
        "currency": "CNY",
    }
    base.update(fields)
    return base


def txn(kind: str, minor: int, date: str = "2026-10-03", currency: str = "CNY") -> dict:
    return {
        "transaction_type": kind,
        "amount_minor": minor,
        "transaction_date": date,
        "currency": currency,
    }


# --- the recorded reference case ------------------------------------------------------------


@pytest.mark.parametrize("month", ["2026-10", "2026-09"])
def test_oracle_reproduces_the_recorded_reference_summary(recorded: dict, month: str) -> None:
    expected = expected_summary(recorded["holdings"], recorded["transactions"], month, "CNY")
    assert compare_summary(expected, recorded[f"summary_{month.replace('-', '_')}"]) == []


def test_recorded_case_has_the_numbers_a_human_can_check_by_hand(recorded: dict) -> None:
    october = recorded["summary_2026_10"]["base"]
    assert october["cash_balance_minor"] == 100050  # 1000.50 cash
    assert october["holdings_value_minor"] == 25000  # 100 units x 2.50
    assert october["holdings_cost_minor"] == 20000  # 100 units x 2.00
    assert october["total_assets_minor"] == 125050
    assert october["income_minor"] == 21350  # deposit 200 + interest 3.5 + dividend 10
    assert october["expense_minor"] == 5525  # withdrawal 50.25 + fee 5
    assert october["net_cashflow_minor"] == 15825
    assert recorded["summary_2026_10"]["excluded_currencies"] == ["USD"]


# --- the oracle must be able to fail ---------------------------------------------------------


@pytest.mark.parametrize("key", AGGREGATE_KEYS)
def test_a_tampered_base_figure_is_reported_with_its_path(recorded: dict, key: str) -> None:
    expected = expected_summary(recorded["holdings"], recorded["transactions"], "2026-10", "CNY")
    actual = copy.deepcopy(recorded["summary_2026_10"])
    actual["base"][key] += 1
    assert compare_summary(expected, actual) == [
        f"base.{key}: expected {expected['base'][key]!r}, got {actual['base'][key]!r}"
    ]


def test_mixing_currencies_into_base_is_caught(recorded: dict) -> None:
    expected = expected_summary(recorded["holdings"], recorded["transactions"], "2026-10", "CNY")
    actual = copy.deepcopy(recorded["summary_2026_10"])
    usd = actual["by_currency"]["USD"]
    for key in ("total_assets_minor", "holdings_value_minor", "income_minor"):
        actual["base"][key] += usd[key]  # what "1:1 mixing" would look like
    diffs = compare_summary(expected, actual)
    assert {d.split(":")[0] for d in diffs} == {
        "base.total_assets_minor",
        "base.holdings_value_minor",
        "base.income_minor",
    }


def test_a_missing_excluded_currency_is_caught(recorded: dict) -> None:
    expected = expected_summary(recorded["holdings"], recorded["transactions"], "2026-10", "CNY")
    actual = copy.deepcopy(recorded["summary_2026_10"])
    actual["excluded_currencies"] = []
    assert any(d.startswith("excluded_currencies") for d in compare_summary(expected, actual))


# --- LED-03 valuation -----------------------------------------------------------------------


def test_cash_is_worth_its_quantity_and_has_no_profit() -> None:
    valuation = value_holding(
        holding(asset_type="cash", quantity=1000.5, average_cost=None, current_price=1.0)
    )
    assert (valuation.market_value_minor, valuation.cost_minor) == (100050, 100050)
    assert valuation.unrealized_profit_minor is None


def test_price_beats_cost_and_profit_is_value_minus_cost() -> None:
    valuation = value_holding(holding(quantity=100.0, average_cost=2.0, current_price=2.5))
    assert (valuation.market_value_minor, valuation.cost_minor) == (25000, 20000)
    assert valuation.unrealized_profit_minor == 5000


def test_a_missing_price_is_estimated_from_cost() -> None:
    valuation = value_holding(holding(quantity=10.0, average_cost=5.0))
    assert valuation.market_value_minor == 5000
    assert valuation.unrealized_profit_minor == 0


def test_a_missing_cost_makes_profit_unknown_not_zero() -> None:
    valuation = value_holding(holding(quantity=10.0, current_price=3.0))
    assert valuation.market_value_minor == 3000
    assert valuation.cost_minor == 0
    assert valuation.unrealized_profit_minor is None


def test_rounding_is_half_up_not_bankers() -> None:
    assert to_minor("1.005") == 101
    assert to_minor("0.015") == 2
    assert to_minor("2.675") == 268


# --- LED-04 cash flow -----------------------------------------------------------------------


def test_only_income_and_expense_types_count_and_net_is_their_difference() -> None:
    transactions = [
        txn("deposit", 1000),
        txn("dividend", 200),
        txn("interest", 30),
        txn("withdrawal", 400),
        txn("fee", 50),
        txn("buy", 9999),
        txn("sell", 8888),
        txn("transfer", 7777),
        txn("other", 6666),
    ]
    summary = expected_summary([], transactions, "2026-10", "CNY")
    assert summary["base"]["income_minor"] == 1230
    assert summary["base"]["expense_minor"] == 450
    assert summary["base"]["net_cashflow_minor"] == 780


def test_the_month_filters_cash_flow_but_never_assets() -> None:
    holdings = [holding(asset_type="cash", quantity=5.0, current_price=1.0)]
    transactions = [txn("deposit", 1000, "2026-09-30"), txn("deposit", 500, "2026-10-01")]
    october = expected_summary(holdings, transactions, "2026-10", "CNY")["base"]
    september = expected_summary(holdings, transactions, "2026-09", "CNY")["base"]
    assert (october["income_minor"], september["income_minor"]) == (500, 1000)
    assert october["total_assets_minor"] == september["total_assets_minor"] == 500


def test_other_currencies_stay_out_of_base_and_are_listed_once_sorted() -> None:
    holdings = [
        holding(currency="USD", quantity=1.0, average_cost=10.0),
        holding(currency="HKD", quantity=1.0, average_cost=20.0),
        holding(currency="CNY", quantity=1.0, average_cost=30.0),
    ]
    summary = expected_summary(holdings, [], "2026-10", "CNY")
    assert summary["base"]["total_assets_minor"] == 3000
    assert summary["excluded_currencies"] == ["HKD", "USD"]
    assert summary["by_currency"]["USD"]["total_assets_minor"] == 1000


def test_the_base_currency_is_reported_even_when_empty() -> None:
    summary = expected_summary([], [], "2026-10", "CNY")
    assert summary["base"] == dict.fromkeys(AGGREGATE_KEYS, 0)
    assert summary["excluded_currencies"] == []


def test_a_currency_seen_only_outside_the_month_is_not_listed() -> None:
    """Documents a boundary the rules leave to the owner: LED-01 note on bucket creation."""
    summary = expected_summary([], [txn("deposit", 100, "2026-09-01", "USD")], "2026-10", "CNY")
    assert summary["excluded_currencies"] == []
