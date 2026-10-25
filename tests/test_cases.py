import json
from typing import Any

import httpx
import pytest
from helpers import PUBLIC, fake_target
from pydantic import ValidationError

from api_test_agent.cases import (
    CaseError,
    CaseModel,
    lookup,
    replay_case,
    resolve,
    validate_case,
)
from api_test_agent.replay import classify
from api_test_agent.spec import Spec
from api_test_agent.targethttp import TargetHttp

SPEC = Spec.load(PUBLIC)
ALL_OPS = list(SPEC.operations)
ALL_CALLERS = ["A", "B", "anonymous", "invalid_token"]
ALL_RULES = SPEC.rule_ids


def case(**overrides: Any) -> CaseModel:
    raw: dict[str, Any] = {
        "case_id": "c1",
        "title": "a transfer across currencies fails",
        "rule_id": "LED-05",
        "claim": "the service rejects it",
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
    raw.update(overrides)
    return CaseModel.model_validate(raw)


def problems(c: CaseModel, **scope: Any) -> list[str]:
    return validate_case(
        c,
        SPEC,
        operations=scope.get("operations", ALL_OPS),
        callers=scope.get("callers", ALL_CALLERS),
        rules=scope.get("rules", ALL_RULES),
    )


# --- the schema -------------------------------------------------------------------------------


def test_a_valid_case_has_no_problems() -> None:
    assert problems(case()) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"case_id": "C1"},  # ids are lowercase
        {"rule_id": "XYZ-01"},
        {"steps": []},
        {"title": "x"},
        {"unknown_field": 1},
    ],
)
def test_malformed_cases_do_not_validate(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        case(**overrides)


def test_numbers_in_path_params_are_accepted_as_text() -> None:
    c = case(
        steps=[
            {
                "id": "s1",
                "operation_id": "accounts.archive",
                "caller": "A",
                "path_params": {"account_id": 123},
                "body": {"version": 1},
                "expect_status": 404,
            }
        ]
    )
    assert c.steps[0].path_params == {"account_id": "123"}


# --- static validation ---------------------------------------------------------------------------


def test_scope_violations_are_reported() -> None:
    assert any("not in the scope" in p for p in problems(case(), rules=["ARC-01"]))
    assert any("not allowed here" in p for p in problems(case(), operations=["accounts.list"]))
    assert any("caller 'A' is not allowed" in p for p in problems(case(), callers=["B"]))


def test_path_params_must_match_the_operation() -> None:
    bad = case(
        steps=[
            {
                "id": "s1",
                "operation_id": "accounts.archive",
                "caller": "A",
                "body": {"version": 1},
                "expect_status": 404,
            }
        ]
    )
    assert any("path_params must be exactly" in p for p in problems(bad))


def test_references_must_point_backwards() -> None:
    steps = [
        {
            "id": "s1",
            "operation_id": "accounts.archive",
            "caller": "A",
            "path_params": {"account_id": "{{s2.data.account_id}}"},
            "body": {"version": 1},
            "expect_status": 200,
        },
        {"id": "s2", "operation_id": "accounts.list", "caller": "A", "expect_status": 200},
    ]
    assert any("not an earlier step" in p for p in problems(case(steps=steps)))


def test_checks_are_parsed_and_must_be_comparisons_over_known_steps() -> None:
    base = case().model_dump()
    ok = CaseModel.model_validate({**base, "checks": [{"expr": "{{s1.status}} == 200"}]})
    assert problems(ok) == []
    syntax = CaseModel.model_validate({**base, "checks": [{"expr": "{{s1.status}} =="}]})
    assert any("check 0" in p for p in problems(syntax))
    arithmetic = CaseModel.model_validate({**base, "checks": [{"expr": "{{s1.status}} + 1"}]})
    assert any("one comparison" in p for p in problems(arithmetic))
    unknown = CaseModel.model_validate({**base, "checks": [{"expr": "{{s9.status}} == 200"}]})
    assert any("unknown step 's9'" in p for p in problems(unknown))


def test_a_case_without_any_expectation_is_refused() -> None:
    bare = case(
        steps=[{"id": "s1", "operation_id": "accounts.list", "caller": "A"}],
    )
    assert any("no expectation" in p for p in problems(bare))


def test_duplicate_step_ids_are_reported() -> None:
    step = {"id": "s1", "operation_id": "accounts.list", "caller": "A", "expect_status": 200}
    assert any("duplicate step id" in p for p in problems(case(steps=[step, step])))


# --- placeholders ---------------------------------------------------------------------------------

RESULTS: dict[str, dict[str, Any]] = {
    "s1": {"status": 200, "data": {"account_id": "acc_1", "version": 3, "items": [{"id": "x"}]}}
}


def test_a_whole_placeholder_keeps_the_type_and_an_embedded_one_becomes_text() -> None:
    assert resolve("{{s1.data.version}}", RESULTS) == 3
    assert resolve("v{{ s1.data.version }}", RESULTS) == "v3"
    nested = {"a": ["{{s1.data.account_id}}", 5], "b": {"c": "{{s1.data.items.0.id}}"}}
    assert resolve(nested, RESULTS) == {"a": ["acc_1", 5], "b": {"c": "x"}}


def test_missing_paths_and_steps_that_did_not_run_raise() -> None:
    with pytest.raises(CaseError, match="does not exist"):
        lookup(RESULTS, "s1", "data.nope")
    with pytest.raises(CaseError, match="has not run"):
        lookup(RESULTS, "s7", "status")
    with pytest.raises(CaseError):
        lookup(RESULTS, "s1", "data.items.4.id")


# --- replay on a fake target ---------------------------------------------------------------


def target(handler: Any = None) -> TargetHttp:
    return TargetHttp(
        "http://target.test", {"A": "ta", "B": "tb"}, SPEC, transport=fake_target(handler)
    )


def answer(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


def test_expectations_that_hold_pass() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(400, {"error": {"code": "TRANSFER_CURRENCY_MISMATCH"}})

    c = case(
        steps=[
            {
                "id": "s1",
                "operation_id": "transactions.batch_upsert",
                "caller": "A",
                "body": {"client_request_id": "x", "items": []},
                "expect_status": 400,
                "expect_error_code": "TRANSFER_CURRENCY_MISMATCH",
            }
        ]
    )
    assert replay_case(c, target(handler)).verdict == "passed"


def test_a_wrong_status_or_error_code_fails_the_case() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return answer(200, {"data": {}})

    step = {
        "id": "s1",
        "operation_id": "accounts.list",
        "caller": "A",
        "expect_status": 400,
    }
    verdict = replay_case(case(steps=[step]), target(ok))
    assert verdict.verdict == "failed" and "expected HTTP 400, got HTTP 200" in verdict.detail

    def wrong_code(request: httpx.Request) -> httpx.Response:
        return answer(409, {"error": {"code": "OTHER"}})

    step = {**step, "expect_status": 409, "expect_error_code": "VERSION_CONFLICT"}
    verdict = replay_case(case(steps=[step]), target(wrong_code))
    assert verdict.verdict == "failed" and "expected error VERSION_CONFLICT" in verdict.detail


def test_later_steps_use_earlier_answers_and_checks_compare_them() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path == "/api/v1/accounts":
            return answer(200, {"data": {"account_id": "acc_9", "version": 4}})
        return answer(200, {"data": {"base": {"income_minor": 10000}}})

    steps = [
        {
            "id": "s1",
            "operation_id": "accounts.upsert",
            "caller": "A",
            "body": {"name": "x", "account_type": "BANK", "currency": "CNY"},
        },
        {
            "id": "s2",
            "operation_id": "accounts.archive",
            "caller": "B",
            "path_params": {"account_id": "{{s1.data.account_id}}"},
            "body": {"version": "{{s1.data.version}}"},
        },
        {
            "id": "s3",
            "operation_id": "ledger.summary",
            "caller": "A",
            "query": {"month": "2026-10"},
        },
    ]
    checks = [
        {"expr": "{{s3.data.base.income_minor}} == 10000"},
        {"expr": "{{s1.data.version}} + 1 == 5"},
    ]
    verdict = replay_case(case(steps=steps, checks=checks), target(handler))
    assert verdict.verdict == "passed"
    archive = seen[1]
    assert archive.url.path == "/api/v1/accounts/acc_9:archive"
    assert json.loads(archive.content) == {"version": 4}  # the number kept its type
    assert archive.headers["authorization"] == "Bearer tb"
    assert seen[2].url.params["month"] == "2026-10"


def test_a_failing_check_fails_the_case_with_its_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(200, {"data": {"base": {"income_minor": 5}}})

    steps = [
        {"id": "s1", "operation_id": "ledger.summary", "caller": "A", "query": {"month": "2026-10"}}
    ]
    checks = [{"expr": "{{s1.data.base.income_minor}} == 0", "message": "income must be gone"}]
    verdict = replay_case(case(steps=steps, checks=checks), target(handler))
    assert verdict.verdict == "failed"
    assert "does not hold (income must be gone)" in verdict.detail


def test_a_field_the_service_does_not_return_is_a_failure_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(200, {"data": {}})

    steps = [
        {"id": "s1", "operation_id": "ledger.summary", "caller": "A", "query": {"month": "2026-10"}}
    ]
    verdict = replay_case(
        case(steps=steps, checks=[{"expr": "{{s1.data.base.x}} == 1"}]), target(handler)
    )
    assert verdict.verdict == "failed"


def test_a_reference_to_a_missing_earlier_value_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(200, {"data": {}})

    steps = [
        {"id": "s1", "operation_id": "accounts.list", "caller": "A"},
        {
            "id": "s2",
            "operation_id": "accounts.archive",
            "caller": "A",
            "path_params": {"account_id": "{{s1.data.account_id}}"},
            "body": {"version": 1},
            "expect_status": 200,
        },
    ]
    verdict = replay_case(case(steps=steps), target(handler))
    assert verdict.verdict == "error" and "step s2" in verdict.detail


def test_a_dropped_connection_is_an_error() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    step = {"id": "s1", "operation_id": "accounts.list", "caller": "A", "expect_status": 200}
    assert replay_case(case(steps=[step]), target(broken)).verdict == "error"


def test_evidence_is_kept_per_step_and_cut_short() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(200, {"data": {"blob": "x" * 5000}})

    step = {"id": "s1", "operation_id": "accounts.list", "caller": "A", "expect_status": 200}
    verdict = replay_case(case(steps=[step]), target(handler))
    assert verdict.evidence[0]["status"] == 200
    assert len(verdict.evidence[0]["body"]) <= 1500


# --- classification ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "target_verdict", "label"),
    [
        ("passed", "failed", "detects"),
        ("passed", "passed", "no_difference"),
        ("failed", "failed", "fails_on_reference"),
        ("failed", "passed", "fails_on_reference"),
        ("error", "failed", "error"),
        ("passed", "error", "error"),
        ("passed", None, "passes"),
        ("failed", None, "fails_on_reference"),
    ],
)
def test_the_label_needs_the_reference_to_pass_and_the_target_to_fail(
    reference: str, target_verdict: str | None, label: str
) -> None:
    from api_test_agent.cases import CaseVerdict

    ref = CaseVerdict("c", reference)  # type: ignore[arg-type]
    tgt = CaseVerdict("c", target_verdict) if target_verdict else None  # type: ignore[arg-type]
    assert classify(ref, tgt) == label
