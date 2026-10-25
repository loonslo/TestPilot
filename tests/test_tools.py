import json
from pathlib import Path
from typing import Any

import httpx
from helpers import PUBLIC, fake_target

from api_test_agent.budget import Budget, DailyLedger, Limits
from api_test_agent.settings import BudgetSettings
from api_test_agent.spec import Spec
from api_test_agent.targethttp import TargetHttp
from api_test_agent.tools import MAX_CASES, TaskScope, ToolBox, tool_schemas

SPEC = Spec.load(PUBLIC)
TOKEN = "super-secret-token-for-A"


def scope(**overrides: Any) -> TaskScope:
    base: dict[str, Any] = {
        "task_id": "dev-99",
        "title": "t",
        "brief": "b",
        "rules": ("LED-05", "LED-06"),
        "operations": ("accounts.upsert", "accounts.list", "transactions.batch_upsert"),
        "callers": ("A", "B"),
        "limits": {"max_http_calls": 3},
    }
    base.update(overrides)
    return TaskScope(**base)


def toolbox(tmp_path: Path, handler: Any = None, task: TaskScope | None = None) -> ToolBox:
    task = task or scope()
    budget = Budget(
        limits=Limits.for_task(BudgetSettings(), task.limits),
        daily=DailyLedger(tmp_path / "u.json"),
        max_requests_per_day=100,
    )
    http = TargetHttp(
        "http://target.test", {"A": TOKEN, "B": "tok-b"}, SPEC, transport=fake_target(handler)
    )
    return ToolBox(task, SPEC, http, budget, tmp_path)


def call(box: ToolBox, name: str, arguments: Any) -> tuple[str, dict[str, Any]]:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    outcome = box.dispatch(name, text)
    return outcome.status, json.loads(outcome.content)


VALID_CASE: dict[str, Any] = {
    "case_id": "c1",
    "title": "cross-currency transfer is rejected",
    "rule_id": "LED-05",
    "claim": "the service refuses",
    "steps": [
        {
            "id": "s1",
            "operation_id": "accounts.upsert",
            "caller": "A",
            "body": {"name": "x", "account_type": "BANK", "currency": "CNY"},
            "expect_status": 200,
        }
    ],
}


# --- schemas ---------------------------------------------------------------------------------


def test_the_schemas_name_the_four_tools_and_enumerate_this_tasks_scope() -> None:
    schemas = tool_schemas(scope())
    assert [s["function"]["name"] for s in schemas] == [
        "search_spec",
        "get_operation",
        "http_call",
        "submit_case",
    ]
    http = schemas[2]["function"]["parameters"]["properties"]
    assert http["operation_id"]["enum"] == list(scope().operations)
    assert http["caller"]["enum"] == ["A", "B"]
    case = schemas[3]["function"]["parameters"]["properties"]
    assert case["rule_id"]["enum"] == ["LED-05", "LED-06"]


def test_the_schemas_only_use_plain_json_schema_that_every_provider_accepts() -> None:
    text = json.dumps(tool_schemas(scope()))
    for feature in ("additionalProperties", "anyOf", "oneOf", "allOf", "$ref", '"type": ['):
        assert feature not in text


# --- protocol ---------------------------------------------------------------------------------


def test_unknown_tools_and_broken_arguments_get_an_answer_not_an_exception(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    status, body = call(box, "delete_everything", {})
    assert status == "error" and "unknown tool" in body["error"]
    status, body = call(box, "search_spec", "{not json")
    assert status == "error" and "not valid JSON" in body["error"]
    status, body = call(box, "search_spec", "[1, 2]")
    assert status == "error" and "JSON object" in body["error"]
    status, body = call(box, "search_spec", {"query": "x", "surprise": 1})
    assert status == "error" and "surprise" in body["error"]


# --- search_spec and get_operation ---------------------------------------------------------------


def test_search_lists_only_operations_of_this_task(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    status, body = call(box, "search_spec", {"query": "archive"})
    ids = {op["operation_id"] for op in body["operations"]}
    assert status == "ok" and ids <= set(scope().operations)
    assert "accounts.archive" not in ids
    _, body = call(box, "search_spec", {"query": "transactions"})
    assert body["operations"][0]["operation_id"] == "transactions.batch_upsert"


def test_get_operation_describes_allowed_operations_only(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    status, body = call(box, "get_operation", {"operation_id": "transactions.batch_upsert"})
    assert status == "ok" and body["method"] == "POST"
    assert body["path"] == "/api/v1/transactions:batch-upsert"
    assert "items" in json.dumps(body["request_body"])
    status, body = call(box, "get_operation", {"operation_id": "accounts.archive"})
    assert status == "error" and "allowed" in body


# --- http_call ----------------------------------------------------------------------------------


def http(box: ToolBox, **arguments: Any) -> tuple[str, dict[str, Any]]:
    base = {"operation_id": "accounts.list", "caller": "A"}
    return call(box, "http_call", {**base, **arguments})


def test_an_allowed_call_returns_the_answer_and_keeps_evidence(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    status, body = http(box)
    assert status == "ok" and body["status"] == 200 and body["evidence_id"] == "e1"
    assert body["body"]["data"]["path"] == "/api/v1/accounts" and body["http_calls_left"] == 2
    evidence = json.loads((tmp_path / "evidence" / "e1.json").read_text(encoding="utf-8"))
    assert evidence["operation_id"] == "accounts.list" and evidence["status"] == 200


def test_permissions_are_checked_before_anything_is_sent(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": {}})

    box = toolbox(tmp_path, handler)
    status, body = http(box, operation_id="accounts.archive", path_params={"account_id": "a"})
    assert status == "error" and "not an operation of this task" in body["error"]
    status, body = http(box, caller="anonymous")
    assert status == "error" and "not allowed here" in body["error"]
    status, body = http(
        box, operation_id="transactions.batch_upsert", caller="B", path_params={"x": "1"}
    )
    assert status == "error" and "path parameters" in body["error"]
    assert sent == []
    assert box._budget.http_calls == 0  # nothing was sent, so nothing was spent


def test_the_http_budget_is_enforced_and_refusals_say_so(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    for _ in range(3):
        assert http(box)[0] == "ok"
    status, body = http(box)
    assert status == "refused" and body["refused"] == "max_http_calls"
    assert box._budget.http_calls == 3


def test_the_token_never_reaches_the_model_or_the_evidence(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    _, body = http(box)
    shown = json.dumps(body)
    stored = (tmp_path / "evidence" / "e1.json").read_text(encoding="utf-8")
    assert TOKEN not in shown and TOKEN not in stored
    assert "Bearer" not in shown and "Bearer" not in stored


def test_a_failed_request_is_an_answer_and_still_costs_a_call(tmp_path: Path) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    box = toolbox(tmp_path, broken)
    status, body = http(box)
    assert status == "error" and "ReadTimeout" in body["error"]
    assert box._budget.http_calls == 1


def test_a_large_answer_is_cut_for_the_model_but_kept_whole_as_evidence(tmp_path: Path) -> None:
    def big(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"blob": "x" * 20_000}})

    box = toolbox(tmp_path, big)
    _, body = http(box)
    assert body["truncated"] is True and len(body["body"]) <= 4096
    evidence = json.loads((tmp_path / "evidence" / "e1.json").read_text(encoding="utf-8"))
    assert len(evidence["response"]["data"]["blob"]) == 20_000


def test_the_error_code_of_a_rejected_call_is_surfaced(tmp_path: Path) -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "VERSION_CONFLICT", "message": "m"}})

    status, body = http(toolbox(tmp_path, reject))
    assert status == "ok" and body["status"] == 409 and body["error_code"] == "VERSION_CONFLICT"


# --- submit_case --------------------------------------------------------------------------------


def test_a_valid_case_is_stored_and_listed_in_cases_json(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    status, body = call(box, "submit_case", VALID_CASE)
    assert status == "ok" and body == {"accepted": True, "case_id": "c1", "cases_submitted": 1}
    stored = json.loads((tmp_path / "cases.json").read_text(encoding="utf-8"))
    assert (
        stored[0]["case_id"] == "c1" and stored[0]["steps"][0]["operation_id"] == "accounts.upsert"
    )


def test_invalid_cases_come_back_with_the_problems(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    status, body = call(box, "submit_case", {**VALID_CASE, "rule_id": "ARC-01"})
    assert status == "error" and body["accepted"] is False
    assert any("not in the scope" in p for p in body["problems"])
    status, body = call(box, "submit_case", {"case_id": "c2"})
    assert status == "error" and "title" in body["problems"]
    assert box.cases == []


def test_the_same_case_id_cannot_be_submitted_twice(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    assert call(box, "submit_case", VALID_CASE)[0] == "ok"
    status, body = call(box, "submit_case", VALID_CASE)
    assert status == "error" and any("already submitted" in p for p in body["problems"])


def test_only_a_few_cases_are_accepted_per_task(tmp_path: Path) -> None:
    box = toolbox(tmp_path)
    for index in range(MAX_CASES):
        assert call(box, "submit_case", {**VALID_CASE, "case_id": f"c{index + 1}"})[0] == "ok"
    status, body = call(box, "submit_case", {**VALID_CASE, "case_id": "c99"})
    assert status == "refused" and body["refused"] == "max_cases"
