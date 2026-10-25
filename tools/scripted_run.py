"""Run a deterministic, scripted "model" through the real runner and the real sandbox.

A smoke test of the whole pipeline that needs no API key and no network: model adapter protocol,
tool dispatch, HTTP calls on a real sandbox, case submission, post-run replay on two builds. The
script plays a competent tester for task dev-02 (transfers and batch writes), which runs on a
build with a currency-mixing transfer defect, and for the same scope on the clean build. It is not
the Agent and it is not evidence about any model. Run from the project root:

    uv run python tools/scripted_run.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api_test_agent import replay, runner
from api_test_agent.budget import DailyLedger
from api_test_agent.layout import Layout
from api_test_agent.model import ModelReply, ToolCall, Usage
from api_test_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[1]

CASE = {
    "case_id": "c1",
    "title": "A transfer between a CNY and a USD account is rejected",
    "rule_id": "LED-05",
    "claim": "Moving money between accounts of different currencies fails with "
    "TRANSFER_CURRENCY_MISMATCH.",
    "steps": [
        {
            "id": "s1",
            "operation_id": "accounts.upsert",
            "caller": "A",
            "body": {"name": "cny", "account_type": "BANK", "currency": "CNY"},
            "expect_status": 200,
        },
        {
            "id": "s2",
            "operation_id": "accounts.upsert",
            "caller": "A",
            "body": {"name": "usd", "account_type": "BANK", "currency": "USD"},
            "expect_status": 200,
        },
        {
            "id": "s3",
            "operation_id": "transactions.batch_upsert",
            "caller": "A",
            "body": {
                "client_request_id": "c1",
                "items": [
                    {
                        "account_id": "{{s1.data.account_id}}",
                        "to_account_id": "{{s2.data.account_id}}",
                        "transaction_type": "TRANSFER",
                        "amount": "10.00",
                        "currency": "CNY",
                        "occurred_at": "2026-10-03",
                    }
                ],
            },
            "expect_status": 400,
            "expect_error_code": "TRANSFER_CURRENCY_MISMATCH",
        },
    ],
}


class ScriptedTester:
    """Reads the tool answers in the conversation, like a model would, and acts on them."""

    model_id = "scripted/tester"

    def __init__(self) -> None:
        self._turn = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> ModelReply:
        self._turn += 1
        answers = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        steps = {
            1: ("search_spec", {"query": "transfer"}),
            2: ("get_operation", {"operation_id": "transactions.batch_upsert"}),
            3: (
                "http_call",
                {
                    "operation_id": "accounts.upsert",
                    "caller": "A",
                    "body": {"name": "cny", "account_type": "BANK", "currency": "CNY"},
                },
            ),
            4: (
                "http_call",
                {
                    "operation_id": "accounts.upsert",
                    "caller": "A",
                    "body": {"name": "usd", "account_type": "BANK", "currency": "USD"},
                },
            ),
        }
        if self._turn in steps:
            name, arguments = steps[self._turn]
            return self._reply(None, [(name, arguments)])
        if self._turn == 5:
            cny, usd = (
                answers[-2]["body"]["data"]["account_id"],
                answers[-1]["body"]["data"]["account_id"],
            )
            transfer = {
                "client_request_id": "probe-1",
                "items": [
                    {
                        "account_id": cny,
                        "to_account_id": usd,
                        "transaction_type": "TRANSFER",
                        "amount": "10.00",
                        "currency": "CNY",
                        "occurred_at": "2026-10-03",
                    }
                ],
            }
            return self._reply(
                None,
                [
                    (
                        "http_call",
                        {
                            "operation_id": "transactions.batch_upsert",
                            "caller": "A",
                            "body": transfer,
                        },
                    )
                ],
            )
        if self._turn == 6:
            status = answers[-1]["status"]
            if status == 400 and answers[-1]["error_code"] == "TRANSFER_CURRENCY_MISMATCH":
                return self._reply(
                    "LED-05 holds: the cross-currency transfer was rejected. No case needed.", []
                )
            return self._reply(None, [("submit_case", CASE)])
        return self._reply(
            "LED-05 is violated: a transfer between a CNY and a USD account was accepted.", []
        )

    @staticmethod
    def _reply(content: str | None, calls: list[tuple[str, dict[str, Any]]]) -> ModelReply:
        tool_calls = tuple(
            ToolCall(id=f"call_{i}", name=name, arguments=json.dumps(arguments))
            for i, (name, arguments) in enumerate(calls)
        )
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in tool_calls
            ]
        return ModelReply(
            content=content,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=Usage(total_tokens=0, cost_usd=0.0),
            served_by="scripted/tester",
            latency_seconds=0.0,
            message=message,
        )


def main() -> int:
    settings = Settings.load(ROOT / "config.local.toml")
    ledger = DailyLedger(Layout.from_settings(settings).runtime / "scripted-usage.json")
    ok = True
    for task, expected_label in (("dev-02", "detects"), ("dev-09", "passes")):
        outcome = runner.run_task(
            settings,
            task,
            model=ScriptedTester(),
            run_id=f"scripted-{task}-{datetime.now(UTC):%m%d-%H%M%S}",
            sleep=lambda s: None,
            ledger=ledger,
        )
        print(
            f"{task}: {outcome.status} ({outcome.stop_reason}), "
            f"cases {outcome.cases}, build {outcome.build}"
        )
        labels = [r["label"] for r in replay.replay_run(settings, outcome.run_dir)["cases"]]
        print(f"  replay labels: {labels or 'no cases'}")
        if task == "dev-02":
            ok &= labels == [expected_label]
        else:
            ok &= not labels  # on the clean build the script found nothing to report
    print("scripted pipeline ok" if ok else "scripted pipeline FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
