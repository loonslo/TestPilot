"""Probe the reference build with a fixed script and record what it actually does.

Host-side tool for A0/A2: the observations back the hand-written response-field notes and show
whether each documented rule holds on the reference build. The Agent never sees this file or its
output. Run from the project root:

    uv run python tools/probe_reference.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api_test_agent import sandbox
from api_test_agent.oracle import compare_summary, expected_summary
from api_test_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
MONTH = "2026-10"


class Probe:
    def __init__(self, scenario: sandbox.Scenario) -> None:
        self.clients = {
            alias: sandbox.target_client(scenario.base_url, token)
            for alias, token in scenario.tokens.items()
        }
        self.clients["anonymous"] = sandbox.target_client(scenario.base_url)
        self.steps: list[dict[str, Any]] = []

    def close(self) -> None:
        for client in self.clients.values():
            client.close()

    def call(
        self,
        label: str,
        actor: str,
        method: str,
        path: str,
        body: Any = None,
        *,
        expect: int | None = None,
        token: str | None = None,
    ) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        resp = self.clients[actor].request(method, path, json=body, headers=headers)
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = {"non_json": resp.text[:200]}
        code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
        self.steps.append(
            {
                "label": label,
                "actor": actor,
                "request": {"method": method, "path": path, "json": body},
                "status": resp.status_code,
                "error_code": code,
                "body": payload,
            }
        )
        flag = (
            f"   <-- expected {expect}" if expect is not None and resp.status_code != expect else ""
        )
        print(f"{label}: {resp.status_code} {code or ''}{flag}")
        return resp.status_code, payload

    def oracle_check(self, label: str, actor: str, month: str) -> list[str]:
        """Re-read both lists and the summary, then compare with the independent arithmetic."""
        _, holdings = self.call(f"{label}:holdings", actor, "GET", "/api/v1/holdings")
        _, transactions = self.call(f"{label}:transactions", actor, "GET", "/api/v1/transactions")
        _, summary = self.call(
            f"{label}:summary", actor, "GET", f"/api/v1/ledger/summary?month={month}"
        )
        reported = summary["data"]
        expected = expected_summary(
            holdings["data"]["items"],
            transactions["data"]["items"],
            month,
            reported["base_currency"],
        )
        differences = compare_summary(expected, reported)
        self.steps.append({"label": f"oracle:{label}", "differences": differences})
        print(f"oracle:{label}: {'ok' if not differences else 'DIFF ' + str(differences[:3])}")
        return differences

    def check(self, label: str, ok: bool, detail: Any = "") -> bool:
        """Record an assertion of the probe itself (not a call)."""
        self.steps.append({"label": f"check:{label}", "ok": bool(ok), "detail": detail})
        print(f"check:{label}: {'ok' if ok else 'FAILED'}")
        return bool(ok)


def data(payload: Any) -> Any:
    return payload["data"]


def tx(kind: str, amount: str, account: str, date: str = "2026-10-03", **extra: Any) -> dict:
    item = {
        "transaction_type": kind,
        "amount": amount,
        "currency": "CNY",
        "occurred_at": date,
        "account_id": account,
    }
    item.update(extra)
    return item


def run(settings: Settings) -> dict[str, Any]:
    scenario = sandbox.start_scenario(settings, "probe-1")
    probe = Probe(scenario)
    try:
        a, b = "A", "B"
        _, r = probe.call(
            "account_cny_bank",
            a,
            "POST",
            "/api/v1/accounts",
            {"name": "CNY Bank", "account_type": "BANK", "currency": "CNY"},
            expect=200,
        )
        c1 = data(r)["account_id"]
        _, r = probe.call(
            "account_cny_broker",
            a,
            "POST",
            "/api/v1/accounts",
            {"name": "CNY Broker", "account_type": "BROKERAGE", "currency": "CNY"},
            expect=200,
        )
        c2 = data(r)["account_id"]
        _, r = probe.call(
            "account_usd_broker",
            a,
            "POST",
            "/api/v1/accounts",
            {"name": "USD Broker", "account_type": "BROKERAGE", "currency": "USD"},
            expect=200,
        )
        u1 = data(r)["account_id"]
        probe.call("accounts_list", a, "GET", "/api/v1/accounts", expect=200)

        holdings = [
            {
                "account_id": c1,
                "name": "CNY cash",
                "asset_type": "CASH",
                "quantity": "1000.50",
                "currency": "CNY",
            },
            {
                "account_id": c1,
                "name": "Fund X",
                "asset_type": "FUND",
                "quantity": "100",
                "average_cost": "2.00",
                "currency": "CNY",
                "metadata": {"current_price": "2.50"},
            },
            {
                "account_id": u1,
                "name": "Stock Y",
                "asset_type": "STOCK",
                "quantity": "10",
                "average_cost": "5",
                "currency": "USD",
            },
        ]
        _, r = probe.call(
            "holdings_upsert",
            a,
            "POST",
            "/api/v1/holdings:batch-upsert",
            {"client_request_id": "probe-h1", "items": holdings},
            expect=200,
        )
        cash_id = data(r)["items"][0]["holding_id"]
        fund_id = data(r)["items"][1]["holding_id"]
        stock_id = data(r)["items"][2]["holding_id"]
        probe.call("holdings_list", a, "GET", "/api/v1/holdings", expect=200)

        txs = [
            tx("DEPOSIT", "200", c1),
            tx("WITHDRAWAL", "50.25", c1, "2026-10-04"),
            tx("FEE", "5", c1, "2026-10-05"),
            tx("INTEREST", "3.5", c1, "2026-10-06"),
            tx("DIVIDEND", "10", c1, "2026-10-07", holding_id=fund_id),
            tx("BUY", "100", c1, "2026-10-08"),
            tx("SELL", "20", c1, "2026-10-09"),
            tx("TRANSFER", "30", c1, "2026-10-10", to_account_id=c2),
            tx("DEPOSIT", "40", u1, "2026-10-11", currency="USD"),
            tx("DEPOSIT", "999", c1, "2026-09-15"),
        ]
        _, r = probe.call(
            "transactions_upsert",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-t1", "items": txs},
            expect=200,
        )
        items = data(r)["items"]
        deposit_id, deposit_version = items[0]["transaction_id"], items[0]["version"]
        dividend_id = items[4]["transaction_id"]
        transfer_id = items[7]["transaction_id"]
        _, listed = probe.call("transactions_list", a, "GET", "/api/v1/transactions", expect=200)
        _, replay = probe.call(
            "transactions_replay_immediately",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-t1", "items": txs},
            expect=200,
        )
        _, relisted = probe.call(
            "transactions_list_after_replay_now", a, "GET", "/api/v1/transactions"
        )
        probe.check(
            "immediate replay returns the first result and creates no duplicates",
            [x["transaction_id"] for x in data(replay)["items"]]
            == [x["transaction_id"] for x in items]
            and len(data(listed)["items"]) == len(data(relisted)["items"]) == len(txs),
        )
        probe.call("summary_2026_10", a, "GET", f"/api/v1/ledger/summary?month={MONTH}", expect=200)
        probe.call("summary_2026_09", a, "GET", "/api/v1/ledger/summary?month=2026-09", expect=200)
        probe.call("summary_bad_month", a, "GET", "/api/v1/ledger/summary?month=2026-13")
        probe.oracle_check("after-setup-oct", a, "2026-10")
        probe.oracle_check("after-setup-sep", a, "2026-09")

        # Business rules from the product documentation.
        probe.call(
            "transfer_cross_currency",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {
                "client_request_id": "probe-t2",
                "items": [tx("TRANSFER", "10", c1, to_account_id=u1)],
            },
            expect=400,
        )
        probe.call(
            "transfer_same_account",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {
                "client_request_id": "probe-t3",
                "items": [tx("TRANSFER", "10", c1, to_account_id=c1)],
            },
        )
        probe.call(
            "to_account_on_non_transfer",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-t4", "items": [tx("DEPOSIT", "10", c1, to_account_id=c2)]},
        )
        probe.call(
            "bad_transaction_type",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-t5", "items": [tx("GIFT", "10", c1)]},
        )

        # Archive guards and soft-archive behaviour.
        probe.call(
            "archive_account_with_holdings",
            a,
            "POST",
            f"/api/v1/accounts/{c1}:archive",
            {"version": 1},
            expect=409,
        )
        probe.call(
            "archive_account_with_transactions",
            a,
            "POST",
            f"/api/v1/accounts/{c2}:archive",
            {"version": 1},
            expect=409,
        )
        probe.call(
            "archive_holding_with_transactions",
            a,
            "POST",
            f"/api/v1/holdings/{fund_id}:archive",
            {"version": 1},
            expect=409,
        )
        probe.call(
            "archive_transaction_stale_version",
            a,
            "POST",
            f"/api/v1/transactions/{deposit_id}:archive",
            {"version": 99},
        )
        probe.call(
            "archive_transaction",
            a,
            "POST",
            f"/api/v1/transactions/{deposit_id}:archive",
            {"version": deposit_version},
            expect=200,
        )
        probe.call(
            "archive_transaction_again",
            a,
            "POST",
            f"/api/v1/transactions/{deposit_id}:archive",
            {"version": deposit_version},
        )
        probe.call("transactions_list_after_archive", a, "GET", "/api/v1/transactions")
        probe.call("summary_after_archive", a, "GET", f"/api/v1/ledger/summary?month={MONTH}")
        probe.call(
            "archive_dividend_transaction",
            a,
            "POST",
            f"/api/v1/transactions/{dividend_id}:archive",
            {"version": 1},
            expect=200,
        )
        probe.call(
            "archive_holding_after_transactions",
            a,
            "POST",
            f"/api/v1/holdings/{fund_id}:archive",
            {"version": 1},
            expect=200,
        )
        probe.call(
            "summary_after_holding_archive", a, "GET", f"/api/v1/ledger/summary?month={MONTH}"
        )

        # Isolation: B uses A's identifiers.
        probe.call("b_accounts_list", b, "GET", "/api/v1/accounts", expect=200)
        probe.call("b_holdings_list", b, "GET", "/api/v1/holdings", expect=200)
        probe.call("b_transactions_list", b, "GET", "/api/v1/transactions", expect=200)
        probe.call("b_summary", b, "GET", f"/api/v1/ledger/summary?month={MONTH}", expect=200)
        probe.call(
            "b_update_a_account",
            b,
            "POST",
            "/api/v1/accounts",
            {
                "account_id": c1,
                "name": "hijack",
                "account_type": "BANK",
                "currency": "CNY",
                "version": 1,
            },
        )
        probe.call(
            "b_archive_a_account", b, "POST", f"/api/v1/accounts/{c2}:archive", {"version": 1}
        )
        probe.call(
            "b_holding_in_a_account",
            b,
            "POST",
            "/api/v1/holdings:batch-upsert",
            {
                "client_request_id": "probe-b1",
                "items": [
                    {
                        "account_id": c1,
                        "name": "x",
                        "asset_type": "CASH",
                        "quantity": "1",
                        "currency": "CNY",
                    }
                ],
            },
        )
        probe.call(
            "b_transaction_in_a_account",
            b,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-b2", "items": [tx("DEPOSIT", "1", c1)]},
        )
        probe.call(
            "b_archive_a_transaction",
            b,
            "POST",
            f"/api/v1/transactions/{items[1]['transaction_id']}:archive",
            {"version": 1},
        )
        probe.call(
            "b_archive_a_holding", b, "POST", f"/api/v1/holdings/{fund_id}:archive", {"version": 1}
        )
        _, r = probe.call(
            "b_create_own_account",
            b,
            "POST",
            "/api/v1/accounts",
            {"name": "B Bank", "account_type": "BANK", "currency": "CNY"},
            expect=200,
        )
        b1 = data(r)["account_id"]
        probe.call(
            "b_transfer_into_a_account",
            b,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-b3", "items": [tx("TRANSFER", "1", b1, to_account_id=c2)]},
        )
        probe.call(
            "b_transfer_out_of_a_account",
            b,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-b4", "items": [tx("TRANSFER", "1", c2, to_account_id=b1)]},
        )
        probe.call("b_transactions_after_attempts", b, "GET", "/api/v1/transactions")
        probe.call("a_summary_after_b_attempts", a, "GET", f"/api/v1/ledger/summary?month={MONTH}")
        probe.oracle_check("a-after-b-attempts", a, MONTH)
        probe.oracle_check("b-own-state", b, MONTH)

        # Idempotency and atomic batches.
        probe.call(
            "idempotent_replay_same_payload",
            a,
            "POST",
            "/api/v1/holdings:batch-upsert",
            {"client_request_id": "probe-h1", "items": holdings},
        )
        probe.call(
            "idempotent_reuse_other_payload",
            a,
            "POST",
            "/api/v1/holdings:batch-upsert",
            {"client_request_id": "probe-h1", "items": holdings[:1]},
        )
        probe.call("holdings_list_after_replay", a, "GET", "/api/v1/holdings")
        probe.call(
            "atomic_batch_with_conflict",
            a,
            "POST",
            "/api/v1/holdings:batch-upsert",
            {
                "client_request_id": "probe-h2",
                "items": [
                    {
                        "account_id": c2,
                        "name": "New cash",
                        "asset_type": "CASH",
                        "quantity": "5",
                        "currency": "CNY",
                    },
                    {
                        "holding_id": cash_id,
                        "account_id": c1,
                        "name": "Stale",
                        "asset_type": "CASH",
                        "quantity": "1",
                        "currency": "CNY",
                        "version": 99,
                    },
                ],
            },
        )
        probe.call("holdings_list_after_atomic_conflict", a, "GET", "/api/v1/holdings")

        # Undocumented behaviour worth knowing before writing rules: is an overdraft accepted?
        code, r = probe.call(
            "overdraft_withdrawal",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {
                "client_request_id": "probe-t6",
                "items": [tx("WITHDRAWAL", "999999", c2, "2026-10-12")],
            },
        )
        overdraft_id = data(r)["items"][0]["transaction_id"] if code == 200 else None

        # More evidence for ARC-02..05, LED-05, LED-08, LED-09 and ISO-02.
        _, r = probe.call(
            "account_empty_create",
            a,
            "POST",
            "/api/v1/accounts",
            {"name": "Empty", "account_type": "OTHER", "currency": "CNY"},
            expect=200,
        )
        c3 = data(r)["account_id"]
        probe.call(
            "archive_empty_account_stale_version",
            a,
            "POST",
            f"/api/v1/accounts/{c3}:archive",
            {"version": 99},
            expect=409,
        )
        probe.call(
            "archive_empty_account",
            a,
            "POST",
            f"/api/v1/accounts/{c3}:archive",
            {"version": 1},
            expect=200,
        )
        _, r = probe.call("accounts_list_after_empty_archive", a, "GET", "/api/v1/accounts")
        probe.check(
            "archived account left the list",
            c3 not in [x["account_id"] for x in data(r)["items"]],
        )
        probe.call(
            "holding_in_archived_account",
            a,
            "POST",
            "/api/v1/holdings:batch-upsert",
            {
                "client_request_id": "probe-h3",
                "items": [
                    {
                        "account_id": c3,
                        "name": "x",
                        "asset_type": "CASH",
                        "quantity": "1",
                        "currency": "CNY",
                    }
                ],
            },
        )
        probe.call(
            "transaction_in_archived_account",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-t7", "items": [tx("DEPOSIT", "1", c3)]},
        )
        probe.call(
            "archive_holding_stale_version",
            a,
            "POST",
            f"/api/v1/holdings/{stock_id}:archive",
            {"version": 99},
            expect=409,
        )
        _, r = probe.call("holdings_list_after_stale_archive", a, "GET", "/api/v1/holdings")
        probe.check(
            "stale-version archive left the holding active",
            stock_id in [x["holding_id"] for x in data(r)["items"]],
        )
        probe.call(
            "archive_overdraft_transaction",
            a,
            "POST",
            f"/api/v1/transactions/{overdraft_id}:archive",
            {"version": 1},
            expect=200,
        )
        probe.call(
            "archive_account_with_transfer_still_active",
            a,
            "POST",
            f"/api/v1/accounts/{c2}:archive",
            {"version": 1},
            expect=409,
        )
        probe.call(
            "archive_transfer_transaction",
            a,
            "POST",
            f"/api/v1/transactions/{transfer_id}:archive",
            {"version": 1},
            expect=200,
        )
        probe.call(
            "archive_account_after_dependants_archived",
            a,
            "POST",
            f"/api/v1/accounts/{c2}:archive",
            {"version": 1},
            expect=200,
        )

        # Observation, not a rule: a replay after a referenced holding was archived.
        _, before = probe.call(
            "transactions_list_before_late_replay", a, "GET", "/api/v1/transactions"
        )
        probe.call(
            "transactions_replay_after_reference_archived",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {"client_request_id": "probe-t1", "items": txs},
        )
        _, after = probe.call(
            "transactions_list_after_late_replay", a, "GET", "/api/v1/transactions"
        )
        probe.check(
            "late replay created no duplicates",
            len(data(before)["items"]) == len(data(after)["items"]),
            {"before": len(data(before)["items"]), "after": len(data(after)["items"])},
        )
        probe.call(
            "batch_with_rejected_transfer",
            a,
            "POST",
            "/api/v1/transactions:batch-upsert",
            {
                "client_request_id": "probe-t8",
                "items": [
                    tx("DEPOSIT", "7", c1, "2026-10-20"),
                    tx("TRANSFER", "1", c1, "2026-10-20", to_account_id=u1),
                ],
            },
            expect=400,
        )
        _, after = probe.call(
            "transactions_list_after_rejected_batch", a, "GET", "/api/v1/transactions"
        )
        probe.check(
            "rejected batch saved nothing, not even its valid item",
            len(data(before)["items"]) == len(data(after)["items"]),
        )
        _, known = probe.call(
            "b_archive_a_transaction_again",
            b,
            "POST",
            f"/api/v1/transactions/{items[2]['transaction_id']}:archive",
            {"version": 1},
        )
        _, unknown = probe.call(
            "b_archive_unknown_id",
            b,
            "POST",
            "/api/v1/transactions/txn_does_not_exist:archive",
            {"version": 1},
        )
        probe.check(
            "foreign id and unknown id are indistinguishable (code and message)",
            known["error"]["code"] == unknown["error"]["code"]
            and known["error"]["message"] == unknown["error"]["message"],
            {"foreign": known["error"], "unknown": unknown["error"]},
        )

        # Authentication on every in-scope read.
        for name, path in (
            ("accounts", "/api/v1/accounts"),
            ("holdings", "/api/v1/holdings"),
            ("transactions", "/api/v1/transactions"),
            ("summary", f"/api/v1/ledger/summary?month={MONTH}"),
        ):
            probe.call(f"no_token_{name}", "anonymous", "GET", path, expect=401)
            probe.call(
                f"bad_token_{name}", "anonymous", "GET", path, token="not-a-token", expect=401
            )
        probe.call("me", a, "GET", "/api/v1/me", expect=200)
        probe.call("health_ready", "anonymous", "GET", "/health/ready", expect=200)
        probe.call("health_version", "anonymous", "GET", "/health/version")
        probe.call("openapi_json", "anonymous", "GET", "/openapi.json")
    finally:
        probe.close()
        sandbox.stop_scenario(settings, scenario)
    return {
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": settings.target.commit,
        "steps": probe.steps,
    }


def main() -> int:
    settings = Settings.load(ROOT / "config.local.toml")
    result = run(settings)
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = artifacts / f"probe-{stamp}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)} ({len(result['steps'])} steps)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
