"""Deterministic rule checks: the host-side oracle behind the benchmark labels.

Each check exercises one rule of ``benchmarks/public/rules.json`` on a running target and raises
``Violation`` when the observed behaviour contradicts the rule's statement. Checks are written
relative to the state they find (deltas, ids they created), so they can share one scenario, and
they run in rule-file order (LED, ARC, ISO) so that records archived by later checks never reach
earlier ones. A failing setup step is a ``HarnessError``: the rule was not judged.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from api_test_agent import sandbox
from api_test_agent.oracle import Valuation, to_minor, value_holding

M_OCT, M_SEP, M_AUG, M_NOV = "2026-10", "2026-09", "2026-08", "2026-11"
ACCOUNTS = "/api/v1/accounts"
HOLDINGS = "/api/v1/holdings"
HOLDINGS_BATCH = "/api/v1/holdings:batch-upsert"
TRANSACTIONS = "/api/v1/transactions"
TRANSACTIONS_BATCH = "/api/v1/transactions:batch-upsert"
SUMMARY = "/api/v1/ledger/summary"
ASSET_KEYS = (
    "cash_balance_minor",
    "holdings_value_minor",
    "holdings_cost_minor",
    "unrealized_profit_minor",
    "total_assets_minor",
)


class Violation(AssertionError):
    """The target's behaviour contradicts a rule."""


class HarnessError(RuntimeError):
    """A setup step failed, so the rule could not be judged."""


@dataclass(frozen=True)
class Reply:
    status: int
    body: Any

    @property
    def error(self) -> dict[str, Any]:
        error = self.body.get("error") if isinstance(self.body, dict) else None
        return error if isinstance(error, dict) else {}

    @property
    def code(self) -> str | None:
        return self.error.get("code")

    @property
    def data(self) -> Any:
        return self.body.get("data") if isinstance(self.body, dict) else None

    @property
    def ok(self) -> bool:
        return self.status == 200


class Actor:
    """One caller of the target: user A, user B, nobody, or somebody with a bad token."""

    def __init__(self, base_url: str, token: str | None) -> None:
        self._client = sandbox.target_client(base_url, token)

    def call(self, method: str, path: str, body: Any = None) -> Reply:
        response = self._client.request(method, path, json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return Reply(response.status_code, payload)

    def close(self) -> None:
        self._client.close()


def hold(
    account_id: str | None,
    kind: str,
    quantity: str,
    name: str,
    *,
    cost: str | None = None,
    price: str | None = None,
    currency: str = "CNY",
    **extra: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "account_id": account_id,
        "name": name,
        "asset_type": kind,
        "quantity": quantity,
        "currency": currency,
    }
    if cost is not None:
        item["average_cost"] = cost
    if price is not None:
        item["metadata"] = {"current_price": price}
    item.update(extra)
    return item


def txn(
    account_id: str,
    kind: str,
    amount: str,
    date: str,
    *,
    currency: str = "CNY",
    **extra: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "account_id": account_id,
        "transaction_type": kind,
        "amount": amount,
        "currency": currency,
        "occurred_at": date,
    }
    item.update(extra)
    return item


class Ctx:
    """Everything a check needs: four callers, builders that fail loudly, and assertions."""

    def __init__(self, scenario: sandbox.Scenario) -> None:
        url = scenario.base_url
        self.a = Actor(url, scenario.tokens["A"])
        self.b = Actor(url, scenario.tokens["B"])
        self.anonymous = Actor(url, None)
        self.invalid = Actor(url, "not-a-token")
        self._counter = 0

    def close(self) -> None:
        for actor in (self.a, self.b, self.anonymous, self.invalid):
            actor.close()

    def next_id(self) -> int:
        self._counter += 1
        return self._counter

    def request_id(self) -> str:
        return f"conf-{self.next_id()}"

    def batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"client_request_id": self.request_id(), "items": items}

    # assertions ---------------------------------------------------------------------------

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            raise Violation(message)

    def setup(self, condition: bool, message: str) -> None:
        if not condition:
            raise HarnessError(message)

    def expect_error(self, reply: Reply, status: int, code: str, what: str) -> None:
        self.require(
            reply.status == status and reply.code == code,
            f"{what}: expected HTTP {status} {code}, got HTTP {reply.status} {reply.code}",
        )

    # builders -----------------------------------------------------------------------------

    def account(self, actor: Actor, currency: str = "CNY", kind: str = "BANK") -> dict[str, Any]:
        reply = actor.call(
            "POST",
            ACCOUNTS,
            {"name": f"conf-{self.next_id()}", "account_type": kind, "currency": currency},
        )
        self.setup(reply.ok, f"create account: HTTP {reply.status} {reply.code}")
        return reply.data

    def holdings(self, actor: Actor, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        reply = actor.call("POST", HOLDINGS_BATCH, self.batch(items))
        self.setup(reply.ok, f"save holdings: HTTP {reply.status} {reply.code}")
        return reply.data["items"]

    def transactions(self, actor: Actor, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        reply = actor.call("POST", TRANSACTIONS_BATCH, self.batch(items))
        self.setup(reply.ok, f"save transactions: HTTP {reply.status} {reply.code}")
        return reply.data["items"]

    def listing(self, actor: Actor, path: str) -> list[dict[str, Any]]:
        reply = actor.call("GET", path)
        self.setup(reply.ok, f"list {path}: HTTP {reply.status} {reply.code}")
        return reply.data["items"]

    def summary(self, actor: Actor, month: str) -> dict[str, Any]:
        reply = actor.call("GET", f"{SUMMARY}?month={month}")
        self.setup(reply.ok, f"summary {month}: HTTP {reply.status} {reply.code}")
        return reply.data

    def state(self, actor: Actor) -> dict[str, Any]:
        """What one user can see: record ids with versions, and the summary figures."""
        summary = self.summary(actor, M_OCT)
        return {
            "accounts": sorted(
                (x["account_id"], x["version"]) for x in self.listing(actor, ACCOUNTS)
            ),
            "holdings": sorted(
                (x["holding_id"], x["version"]) for x in self.listing(actor, HOLDINGS)
            ),
            "transactions": sorted(
                (x["transaction_id"], x["version"]) for x in self.listing(actor, TRANSACTIONS)
            ),
            "base": summary["base"],
            "by_currency": summary["by_currency"],
            "excluded_currencies": summary["excluded_currencies"],
        }


CHECKS: dict[str, Callable[[Ctx], None]] = {}


def check(rule_id: str) -> Callable[[Callable[[Ctx], None]], Callable[[Ctx], None]]:
    def register(function: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
        CHECKS[rule_id] = function
        return function

    return register


def reported_assets(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-currency asset figures summed from the per-holding values the service itself reports."""
    totals: dict[str, dict[str, int]] = {}
    for item in items:
        current = totals.setdefault(
            item["currency"], dict.fromkeys(("cash", "value", "cost", "total"), 0)
        )
        value = item["market_value_minor"]
        current["total"] += value
        if item["asset_type"] == "cash":
            current["cash"] += value
        else:
            current["value"] += value
            current["cost"] += item["cost_minor"]
    return totals


def asset_figures(summary: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        currency: {key: totals[key] for key in ASSET_KEYS}
        for currency, totals in summary["by_currency"].items()
    }


# ---------------------------------------------------------------------------- F1: ledger summary


@check("LED-01")
def led_01(ctx: Ctx) -> None:
    a = ctx.a
    cny, usd, hkd = (ctx.account(a, currency) for currency in ("CNY", "USD", "HKD"))
    ctx.holdings(
        a,
        [
            hold(cny["account_id"], "CASH", "100", "cash"),
            hold(cny["account_id"], "FUND", "10", "fund", cost="3", price="4"),
            hold(usd["account_id"], "STOCK", "5", "stock", cost="20", currency="USD"),
            hold(usd["account_id"], "CASH", "50", "usd cash", currency="USD"),
            hold(hkd["account_id"], "BOND", "2", "bond", cost="30", currency="HKD"),
        ],
    )
    summary = ctx.summary(a, M_OCT)
    by_currency, base = summary["by_currency"], summary["base_currency"]
    names = {
        "cash": "cash_balance_minor",
        "value": "holdings_value_minor",
        "cost": "holdings_cost_minor",
        "total": "total_assets_minor",
    }
    for currency, want in reported_assets(ctx.listing(a, HOLDINGS)).items():
        got = by_currency.get(currency)
        ctx.require(got is not None, f"{currency} holdings exist but by_currency has no {currency}")
        for short, field in names.items():
            ctx.require(
                got[field] == want[short],
                f"by_currency[{currency}].{field} is {got[field]}, "
                f"but the {currency} holdings add up to {want[short]}",
            )
    ctx.require(
        summary["base"] == by_currency[base], "base differs from by_currency[base_currency]"
    )
    others = sorted(currency for currency in by_currency if currency != base)
    ctx.require(
        summary["excluded_currencies"] == others,
        f"excluded_currencies is {summary['excluded_currencies']}, expected {others}",
    )


@check("LED-02")
def led_02(ctx: Ctx) -> None:
    a = ctx.a
    account = ctx.account(a)["account_id"]
    ctx.holdings(
        a,
        [
            hold(account, "CASH", "250.25", "cash"),
            hold(account, "FUND", "3", "fund", cost="10", price="12"),
        ],
    )
    first = ctx.summary(a, M_OCT)
    for currency, totals in first["by_currency"].items():
        ctx.require(
            totals["total_assets_minor"]
            == totals["cash_balance_minor"] + totals["holdings_value_minor"],
            f"{currency}: total_assets_minor is not cash plus holdings value",
        )
    ctx.require(
        asset_figures(first) == asset_figures(ctx.summary(a, "2026-03")),
        "assets change with the requested month",
    )
    ctx.transactions(
        a,
        [
            txn(account, "DEPOSIT", "1000.00", "2026-10-02"),
            txn(account, "WITHDRAWAL", "300.00", "2026-10-03"),
            txn(account, "BUY", "50.00", "2026-10-04"),
            txn(account, "SELL", "20.00", "2026-10-05"),
        ],
    )
    ctx.require(
        asset_figures(first) == asset_figures(ctx.summary(a, M_OCT)),
        "transactions changed the assets",
    )


@check("LED-03")
def led_03(ctx: Ctx) -> None:
    a = ctx.a
    account = ctx.account(a)["account_id"]
    cases = [  # kind, quantity, average cost, price
        ("CASH", "1000.50", None, None),
        ("FUND", "100", "2.00", "2.50"),
        ("STOCK", "10", "5", None),
        ("FUND", "10", None, "3"),
        ("FUND", "1.005", "1", "1"),
        ("FUND", "0.015", "1", "1"),
        ("FUND", "7", None, None),
    ]
    saved = ctx.holdings(
        a,
        [
            hold(account, kind, quantity, f"valuation-{index}", cost=cost, price=price)
            for index, (kind, quantity, cost, price) in enumerate(cases)
        ],
    )
    for (kind, quantity, cost, price), item in zip(cases, saved, strict=True):
        expected: Valuation = value_holding(
            {
                "asset_type": kind.lower(),
                "quantity": quantity,
                "average_cost": cost,
                "current_price": price,
            }
        )
        label = f"{kind} quantity {quantity} cost {cost} price {price}"
        for field, want in (
            ("market_value_minor", expected.market_value_minor),
            ("cost_minor", expected.cost_minor),
            ("unrealized_profit_minor", expected.unrealized_profit_minor),
        ):
            ctx.require(item[field] == want, f"{label}: {field} is {item[field]}, expected {want}")
        if kind != "CASH":
            status = "priced" if price is not None else "estimated"
            ctx.require(
                item["valuation_status"] == status,
                f"{label}: valuation_status is {item['valuation_status']}, expected {status}",
            )


@check("LED-04")
def led_04(ctx: Ctx) -> None:
    a = ctx.a
    first, second = ctx.account(a)["account_id"], ctx.account(a)["account_id"]
    months = (M_AUG, M_SEP, M_OCT, M_NOV)
    before = {month: ctx.summary(a, month)["base"] for month in months}
    ctx.transactions(
        a,
        [
            txn(first, "DEPOSIT", "100.00", "2026-10-05"),
            txn(first, "DIVIDEND", "20.00", "2026-10-06"),
            txn(first, "INTEREST", "3.00", "2026-10-07"),
            txn(first, "WITHDRAWAL", "40.00", "2026-10-08"),
            txn(first, "FEE", "5.00", "2026-10-09"),
            txn(first, "BUY", "70.00", "2026-10-10"),
            txn(first, "SELL", "60.00", "2026-10-11"),
            txn(first, "TRANSFER", "10.00", "2026-10-12", to_account_id=second),
            txn(first, "OTHER", "9.00", "2026-10-13"),
            txn(first, "WITHDRAWAL", "-8.00", "2026-10-14"),
            txn(first, "DEPOSIT", "2.00", "2026-10-31"),
            txn(first, "DEPOSIT", "11.00", "2026-11-01"),
            txn(first, "DEPOSIT", "1000.00", "2026-09-15"),
            txn(first, "WITHDRAWAL", "500.00", "2026-09-16"),
        ],
    )
    expected = {  # month -> (income, expense) in minor units
        M_AUG: (0, 0),
        M_SEP: (100000, 50000),
        M_OCT: (to_minor("125.00"), to_minor("53.00")),
        M_NOV: (1100, 0),
    }
    for month, (income, expense) in expected.items():
        after = ctx.summary(a, month)["base"]
        got = (
            after["income_minor"] - before[month]["income_minor"],
            after["expense_minor"] - before[month]["expense_minor"],
            after["net_cashflow_minor"] - before[month]["net_cashflow_minor"],
        )
        ctx.require(
            got == (income, expense, income - expense),
            f"{month}: income, expense and net changed by {got}, "
            f"expected {(income, expense, income - expense)}",
        )


@check("LED-05")
def led_05(ctx: Ctx) -> None:
    a = ctx.a
    cny_one, cny_two, usd = ctx.account(a), ctx.account(a), ctx.account(a, "USD")
    before = len(ctx.listing(a, TRANSACTIONS))
    cross = a.call(
        "POST",
        TRANSACTIONS_BATCH,
        ctx.batch(
            [
                txn(
                    cny_one["account_id"],
                    "TRANSFER",
                    "10.00",
                    "2026-10-03",
                    to_account_id=usd["account_id"],
                )
            ]
        ),
    )
    ctx.expect_error(cross, 400, "TRANSFER_CURRENCY_MISMATCH", "transfer between currencies")
    missing = a.call(
        "POST",
        TRANSACTIONS_BATCH,
        ctx.batch([txn(cny_one["account_id"], "TRANSFER", "10.00", "2026-10-03")]),
    )
    ctx.require(
        missing.status == 400, f"a transfer without to_account_id gave HTTP {missing.status}"
    )
    same_currency = a.call(
        "POST",
        TRANSACTIONS_BATCH,
        ctx.batch(
            [
                txn(
                    cny_one["account_id"],
                    "TRANSFER",
                    "10.00",
                    "2026-10-03",
                    to_account_id=cny_two["account_id"],
                )
            ]
        ),
    )
    ctx.setup(same_currency.ok, f"same-currency transfer: HTTP {same_currency.status}")
    ctx.require(
        len(ctx.listing(a, TRANSACTIONS)) == before + 1,
        "a rejected transfer left records behind",
    )


@check("LED-06")
def led_06(ctx: Ctx) -> None:
    a = ctx.a
    one, two = ctx.account(a)["account_id"], ctx.account(a)["account_id"]
    same = a.call(
        "POST",
        TRANSACTIONS_BATCH,
        ctx.batch([txn(one, "TRANSFER", "1.00", "2026-10-03", to_account_id=one)]),
    )
    ctx.expect_error(same, 400, "VALIDATION_ERROR", "transfer to the same account")
    stray = a.call(
        "POST",
        TRANSACTIONS_BATCH,
        ctx.batch([txn(one, "DEPOSIT", "1.00", "2026-10-03", to_account_id=two)]),
    )
    ctx.expect_error(stray, 400, "VALIDATION_ERROR", "deposit carrying to_account_id")


@check("LED-07")
def led_07(ctx: Ctx) -> None:
    for month in ("2026-13", "2026-00", "2026-1", "26-10", "2026/10", "abcd-ef"):
        ctx.expect_error(
            ctx.a.call("GET", f"{SUMMARY}?month={month}"), 400, "VALIDATION_ERROR", f"month={month}"
        )
    for month in ("2026-10", "2026-12"):
        reply = ctx.a.call("GET", f"{SUMMARY}?month={month}")
        ctx.require(reply.ok, f"month={month} was rejected with HTTP {reply.status}")


@check("LED-08")
def led_08(ctx: Ctx) -> None:
    a = ctx.a
    account = ctx.account(a)["account_id"]
    holding = ctx.holdings(a, [hold(account, "CASH", "5", "existing")])[0]
    deposit = ctx.transactions(a, [txn(account, "DEPOSIT", "5.00", "2026-10-04")])[0]
    names_before = sorted(x["name"] for x in ctx.listing(a, HOLDINGS))
    transactions_before = len(ctx.listing(a, TRANSACTIONS))
    stale_holding = hold(
        account,
        "CASH",
        "1",
        "stale",
        holding_id=holding["holding_id"],
        version=holding["version"] + 50,
    )
    reply = a.call(
        "POST", HOLDINGS_BATCH, ctx.batch([hold(account, "CASH", "9", "fresh"), stale_holding])
    )
    ctx.require(reply.status >= 400, "a holdings batch with an outdated item was accepted")
    ctx.require(
        sorted(x["name"] for x in ctx.listing(a, HOLDINGS)) == names_before,
        "a failed holdings batch left part of its items behind",
    )
    stale_deposit = txn(
        account,
        "DEPOSIT",
        "6.00",
        "2026-10-05",
        transaction_id=deposit["transaction_id"],
        version=deposit["version"] + 50,
    )
    reply = a.call(
        "POST",
        TRANSACTIONS_BATCH,
        ctx.batch([txn(account, "DEPOSIT", "13.00", "2026-10-05"), stale_deposit]),
    )
    ctx.require(reply.status >= 400, "a transactions batch with an outdated item was accepted")
    ctx.require(
        len(ctx.listing(a, TRANSACTIONS)) == transactions_before,
        "a failed transactions batch left part of its items behind",
    )


@check("LED-09")
def led_09(ctx: Ctx) -> None:
    a = ctx.a
    account = ctx.account(a)["account_id"]
    for path, listing_path, key, item in (
        (HOLDINGS_BATCH, HOLDINGS, "holding_id", hold(account, "CASH", "7", "replay")),
        (
            TRANSACTIONS_BATCH,
            TRANSACTIONS,
            "transaction_id",
            txn(account, "DEPOSIT", "7.00", "2026-10-06"),
        ),
    ):
        body = ctx.batch([item])
        first = a.call("POST", path, body)
        ctx.setup(first.ok, f"first send to {path}: HTTP {first.status} {first.code}")
        count = len(ctx.listing(a, listing_path))
        again = a.call("POST", path, body)
        ctx.require(again.ok, f"replay of {path} failed: HTTP {again.status} {again.code}")
        ctx.require(
            [x[key] for x in again.data["items"]] == [x[key] for x in first.data["items"]],
            f"replay of {path} returned different records",
        )
        ctx.require(
            len(ctx.listing(a, listing_path)) == count, f"replay of {path} created duplicates"
        )


# ---------------------------------------------------------------------------- F2: archive


def archive(actor: Actor, kind: str, record_id: str, version: int) -> Reply:
    return actor.call("POST", f"/api/v1/{kind}/{record_id}:archive", {"version": version})


@check("ARC-01")
def arc_01(ctx: Ctx) -> None:
    a = ctx.a
    account = ctx.account(a)["account_id"]
    fund = ctx.holdings(a, [hold(account, "FUND", "10", "fund", cost="3", price="4")])[0]
    deposit, withdrawal = ctx.transactions(
        a,
        [
            txn(account, "DEPOSIT", "100.00", "2026-10-05"),
            txn(account, "WITHDRAWAL", "30.00", "2026-10-06"),
        ],
    )
    before = ctx.summary(a, M_OCT)["base"]
    reply = archive(a, "transactions", deposit["transaction_id"], deposit["version"])
    ctx.require(
        reply.ok
        and reply.data.get("archived") is True
        and reply.data.get("version") == deposit["version"] + 1,
        f"archiving a transaction answered HTTP {reply.status} {reply.data}",
    )
    ctx.require(
        deposit["transaction_id"]
        not in {x["transaction_id"] for x in ctx.listing(a, TRANSACTIONS)},
        "an archived transaction is still listed",
    )
    after = ctx.summary(a, M_OCT)["base"]
    ctx.require(
        after["income_minor"] - before["income_minor"] == -10000
        and after["expense_minor"] == before["expense_minor"]
        and after["net_cashflow_minor"] - before["net_cashflow_minor"] == -10000,
        "archiving a deposit did not lower the month's income by exactly that deposit",
    )
    archive(a, "transactions", withdrawal["transaction_id"], withdrawal["version"])
    final = ctx.summary(a, M_OCT)["base"]
    ctx.require(
        final["expense_minor"] - after["expense_minor"] == -3000,
        "archiving a withdrawal did not lower the month's expense by exactly that withdrawal",
    )
    reply = archive(a, "holdings", fund["holding_id"], fund["version"])
    ctx.require(reply.ok, f"archiving a holding answered HTTP {reply.status} {reply.code}")
    ctx.require(
        fund["holding_id"] not in {x["holding_id"] for x in ctx.listing(a, HOLDINGS)},
        "an archived holding is still listed",
    )
    last = ctx.summary(a, M_OCT)["base"]
    ctx.require(
        last["holdings_value_minor"] - final["holdings_value_minor"] == -4000
        and last["holdings_cost_minor"] - final["holdings_cost_minor"] == -3000
        and last["total_assets_minor"] - final["total_assets_minor"] == -4000
        and last["cash_balance_minor"] == final["cash_balance_minor"],
        "archiving a holding did not remove exactly its value and cost from the summary",
    )


@check("ARC-02")
def arc_02(ctx: Ctx) -> None:
    a = ctx.a
    account = ctx.account(a)["account_id"]
    empty = ctx.account(a)
    holding = ctx.holdings(a, [hold(account, "CASH", "3", "h")])[0]
    deposit = ctx.transactions(a, [txn(account, "DEPOSIT", "3.00", "2026-10-07")])[0]
    cases = [
        (
            "transaction",
            "transactions",
            TRANSACTIONS,
            "transaction_id",
            deposit["transaction_id"],
            deposit["version"],
        ),
        ("holding", "holdings", HOLDINGS, "holding_id", holding["holding_id"], holding["version"]),
        ("account", "accounts", ACCOUNTS, "account_id", empty["account_id"], empty["version"]),
    ]
    for label, kind, listing_path, key, record_id, version in cases:
        for wrong in (version + 1, version + 98):
            ctx.expect_error(
                archive(a, kind, record_id, wrong),
                409,
                "VERSION_CONFLICT",
                f"{label} archived with version {wrong}",
            )
        ctx.require(
            record_id in {x[key] for x in ctx.listing(a, listing_path)},
            f"a {label} left the list after a rejected archive",
        )
        reply = archive(a, kind, record_id, version)
        ctx.require(
            reply.ok,
            f"{label}: archive with the current version gave HTTP {reply.status} {reply.code}",
        )


@check("ARC-03")
def arc_03(ctx: Ctx) -> None:
    a = ctx.a
    with_holding = ctx.account(a)
    ctx.holdings(a, [hold(with_holding["account_id"], "CASH", "1", "dependant")])
    with_transaction = ctx.account(a)
    paid = ctx.transactions(
        a, [txn(with_transaction["account_id"], "DEPOSIT", "1.00", "2026-10-08")]
    )[0]
    source, target = ctx.account(a), ctx.account(a)
    transfer = ctx.transactions(
        a,
        [
            txn(
                source["account_id"],
                "TRANSFER",
                "4.00",
                "2026-10-08",
                to_account_id=target["account_id"],
            )
        ],
    )[0]
    both = ctx.account(a)
    ctx.holdings(a, [hold(both["account_id"], "CASH", "1", "dependant")])
    ctx.transactions(a, [txn(both["account_id"], "DEPOSIT", "1.00", "2026-10-08")])

    def archive_account(account: dict[str, Any]) -> Reply:
        return archive(a, "accounts", account["account_id"], account["version"])

    ctx.expect_error(
        archive_account(with_holding), 409, "ACCOUNT_HAS_HOLDINGS", "account with a holding"
    )
    ctx.expect_error(
        archive_account(with_transaction),
        409,
        "ACCOUNT_HAS_TRANSACTIONS",
        "account with a transaction",
    )
    ctx.expect_error(archive_account(source), 409, "ACCOUNT_HAS_TRANSACTIONS", "transfer source")
    ctx.expect_error(archive_account(target), 409, "ACCOUNT_HAS_TRANSACTIONS", "transfer target")
    ctx.expect_error(
        archive_account(both), 409, "ACCOUNT_HAS_HOLDINGS", "account with both (holdings first)"
    )
    active = {x["account_id"] for x in ctx.listing(a, ACCOUNTS)}
    ctx.require(
        all(
            x["account_id"] in active
            for x in (with_holding, with_transaction, source, target, both)
        ),
        "a rejected archive removed the account anyway",
    )
    ctx.setup(
        archive(a, "transactions", paid["transaction_id"], paid["version"]).ok, "archive deposit"
    )
    reply = archive_account(with_transaction)
    ctx.require(reply.ok, f"account with its dependants archived: HTTP {reply.status} {reply.code}")
    ctx.setup(
        archive(a, "transactions", transfer["transaction_id"], transfer["version"]).ok,
        "archive transfer",
    )
    ctx.require(
        archive_account(target).ok,
        "the transfer target could not be archived after its transfer was",
    )


@check("ARC-04")
def arc_04(ctx: Ctx) -> None:
    a = ctx.a
    account = ctx.account(a)["account_id"]
    holding = ctx.holdings(a, [hold(account, "FUND", "2", "fund", cost="1", price="1")])[0]
    dividend = ctx.transactions(
        a, [txn(account, "DIVIDEND", "2.00", "2026-10-09", holding_id=holding["holding_id"])]
    )[0]
    ctx.expect_error(
        archive(a, "holdings", holding["holding_id"], holding["version"]),
        409,
        "HOLDING_HAS_TRANSACTIONS",
        "holding referenced by a transaction",
    )
    ctx.setup(
        archive(a, "transactions", dividend["transaction_id"], dividend["version"]).ok,
        "archive dividend",
    )
    reply = archive(a, "holdings", holding["holding_id"], holding["version"])
    ctx.require(
        reply.ok, f"holding archived after its transactions: HTTP {reply.status} {reply.code}"
    )


@check("ARC-05")
def arc_05(ctx: Ctx) -> None:
    a = ctx.a
    account = ctx.account(a)["account_id"]
    deposit = ctx.transactions(a, [txn(account, "DEPOSIT", "1.00", "2026-10-10")])[0]
    ctx.setup(
        archive(a, "transactions", deposit["transaction_id"], deposit["version"]).ok,
        "archive deposit",
    )
    ctx.expect_error(
        archive(a, "transactions", deposit["transaction_id"], deposit["version"] + 1),
        404,
        "NOT_FOUND",
        "archiving an archived transaction",
    )
    for kind in ("accounts", "holdings", "transactions"):
        ctx.expect_error(
            archive(a, kind, f"{kind[:3]}_does_not_exist", 1),
            404,
            "NOT_FOUND",
            f"unknown {kind} id",
        )
    gone = ctx.account(a)
    ctx.setup(
        archive(a, "accounts", gone["account_id"], gone["version"]).ok, "archive empty account"
    )
    ctx.expect_error(
        a.call("POST", HOLDINGS_BATCH, ctx.batch([hold(gone["account_id"], "CASH", "1", "x")])),
        404,
        "NOT_FOUND",
        "holding in an archived account",
    )
    ctx.expect_error(
        a.call(
            "POST",
            TRANSACTIONS_BATCH,
            ctx.batch([txn(gone["account_id"], "DEPOSIT", "1.00", "2026-10-10")]),
        ),
        404,
        "NOT_FOUND",
        "transaction in an archived account",
    )


# ---------------------------------------------------------------------------- F3: isolation


@check("ISO-01")
def iso_01(ctx: Ctx) -> None:
    a, b = ctx.a, ctx.b
    b_before = ctx.state(b)
    account = ctx.account(a)
    holding = ctx.holdings(a, [hold(account["account_id"], "CASH", "11", "mine")])[0]
    deposit = ctx.transactions(a, [txn(account["account_id"], "DEPOSIT", "11.00", "2026-10-11")])[0]
    ctx.require(ctx.state(b) == b_before, "what user B reads changed after user A wrote")
    a_before = ctx.state(a)
    b_account = ctx.account(b)["account_id"]
    ctx.holdings(b, [hold(b_account, "CASH", "22", "theirs")])
    ctx.transactions(b, [txn(b_account, "DEPOSIT", "22.00", "2026-10-11")])
    ctx.require(ctx.state(a) == a_before, "what user A reads changed after user B wrote")
    b_state = ctx.state(b)
    ctx.require(
        not set(dict(b_state["accounts"])) & set(dict(a_before["accounts"])),
        "user B's account list contains user A's accounts",
    )
    # Whatever user B tries on user A's records, what user A reads must stay the same.
    for kind, record_id, version in (
        ("transactions", deposit["transaction_id"], deposit["version"]),
        ("holdings", holding["holding_id"], holding["version"]),
        ("accounts", account["account_id"], account["version"]),
    ):
        archive(b, kind, record_id, version)
    ctx.require(
        ctx.state(a) == a_before,
        "what user A reads changed after user B tried to archive user A's records",
    )


@check("ISO-02")
def iso_02(ctx: Ctx) -> None:
    a, b = ctx.a, ctx.b
    free = ctx.account(a)
    busy = ctx.account(a)
    holding = ctx.holdings(a, [hold(busy["account_id"], "CASH", "2", "iso")])[0]
    deposit = ctx.transactions(a, [txn(busy["account_id"], "DEPOSIT", "2.00", "2026-10-12")])[0]
    before = ctx.state(a)
    attempts = {
        "update account": b.call(
            "POST",
            ACCOUNTS,
            {
                "account_id": free["account_id"],
                "name": "hijack",
                "account_type": "BANK",
                "currency": "CNY",
                "version": free["version"],
            },
        ),
        "archive account": archive(b, "accounts", free["account_id"], free["version"]),
        "archive holding": archive(b, "holdings", holding["holding_id"], holding["version"]),
        "archive transaction": archive(
            b, "transactions", deposit["transaction_id"], deposit["version"]
        ),
    }
    unknown = archive(b, "accounts", "acc_does_not_exist", 1)
    for label, reply in attempts.items():
        ctx.expect_error(reply, 404, "NOT_FOUND", f"user B, {label} of user A")
        ctx.require(
            reply.error.get("message") == unknown.error.get("message"),
            f"user B, {label}: the answer differs from the answer for an unknown id",
        )
    ctx.require(ctx.state(a) == before, "user A's data changed after user B's attempts")


@check("ISO-03")
def iso_03(ctx: Ctx) -> None:
    a, b = ctx.a, ctx.b
    a_account, b_account = ctx.account(a)["account_id"], ctx.account(b)["account_id"]
    before = ctx.state(b)
    attempts = {
        "holding in A's account": b.call(
            "POST", HOLDINGS_BATCH, ctx.batch([hold(a_account, "CASH", "1", "x")])
        ),
        "transaction in A's account": b.call(
            "POST", TRANSACTIONS_BATCH, ctx.batch([txn(a_account, "DEPOSIT", "1.00", "2026-10-13")])
        ),
        "transfer into A's account": b.call(
            "POST",
            TRANSACTIONS_BATCH,
            ctx.batch([txn(b_account, "TRANSFER", "1.00", "2026-10-13", to_account_id=a_account)]),
        ),
        "transfer out of A's account": b.call(
            "POST",
            TRANSACTIONS_BATCH,
            ctx.batch([txn(a_account, "TRANSFER", "1.00", "2026-10-13", to_account_id=b_account)]),
        ),
    }
    for label, reply in attempts.items():
        ctx.expect_error(reply, 404, "NOT_FOUND", label)
    ctx.require(ctx.state(b) == before, "a rejected reference to A's account left records behind")


@check("ISO-04")
def iso_04(ctx: Ctx) -> None:
    reads = (ACCOUNTS, HOLDINGS, TRANSACTIONS, f"{SUMMARY}?month={M_OCT}")
    for actor, code in ((ctx.anonymous, "UNAUTHORIZED"), (ctx.invalid, "TOKEN_INVALID")):
        for path in reads:
            ctx.expect_error(actor.call("GET", path), 401, code, f"GET {path} as {code}")
        writes = (
            (ACCOUNTS, {"name": "x", "account_type": "BANK", "currency": "CNY"}),
            (HOLDINGS_BATCH, ctx.batch([hold(None, "CASH", "1", "x")])),
            (TRANSACTIONS_BATCH, ctx.batch([txn("acc_x", "DEPOSIT", "1.00", "2026-10-14")])),
            ("/api/v1/accounts/acc_x:archive", {"version": 1}),
            ("/api/v1/holdings/hol_x:archive", {"version": 1}),
            ("/api/v1/transactions/txn_x:archive", {"version": 1}),
        )
        for path, body in writes:
            ctx.expect_error(actor.call("POST", path, body), 401, code, f"POST {path} as {code}")


# ---------------------------------------------------------------------------- runner


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    outcome: Literal["pass", "violation", "error"]
    detail: str = ""


def rule_order(rules_path: Path) -> list[str]:
    """Rule ids in the order of rules.json (LED, ARC, ISO)."""
    return [rule["id"] for rule in json.loads(rules_path.read_text(encoding="utf-8"))["rules"]]


def run_conformance(scenario: sandbox.Scenario, order: Sequence[str]) -> list[RuleResult]:
    """Run the checks for ``order`` against a running scenario and report one result per rule."""
    ctx = Ctx(scenario)
    results: list[RuleResult] = []
    try:
        for rule_id in order:
            function = CHECKS.get(rule_id)
            if function is None:
                results.append(RuleResult(rule_id, "error", "no check implemented"))
                continue
            try:
                function(ctx)
            except Violation as exc:
                results.append(RuleResult(rule_id, "violation", str(exc)))
            except Exception as exc:  # a broken setup or an unexpected response shape
                results.append(RuleResult(rule_id, "error", f"{type(exc).__name__}: {exc}"))
            else:
                results.append(RuleResult(rule_id, "pass"))
    finally:
        ctx.close()
    return results
