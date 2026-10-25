"""Test cases the Agent submits, and the deterministic replay that judges them.

A case is data, never code: steps name operations and callers, later steps and checks refer to
earlier answers with ``{{step.path}}`` placeholders, and checks are small comparisons in the
expression language of ``expr.py``. A case states the behaviour a *rule requires*, so it passes on
a correct service and fails on a faulty one; the replayer decides which.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from api_test_agent import expr
from api_test_agent.spec import Spec
from api_test_agent.targethttp import CALLERS, CallError, TargetHttp

_ID = r"^[a-z][a-z0-9_]{0,23}$"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,23})\.([A-Za-z0-9_.\-]+)\s*\}\}")
_WHOLE = re.compile(r"^\s*\{\{\s*([a-z][a-z0-9_]{0,23})\.([A-Za-z0-9_.\-]+)\s*\}\}\s*$")


class CaseError(ValueError):
    """A case cannot be replayed: an unresolved reference or a value of the wrong kind."""


class StepModel(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    id: str = Field(pattern=_ID)
    operation_id: str
    caller: Literal["A", "B", "anonymous", "invalid_token"]
    path_params: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    expect_status: int | None = Field(default=None, ge=100, le=599)
    expect_error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,60}$")


class CheckModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expr: str = Field(min_length=3, max_length=expr.MAX_LENGTH)
    message: str = Field(default="", max_length=300)


class CaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=_ID)
    title: str = Field(min_length=3, max_length=160)
    rule_id: str = Field(pattern=r"^(LED|ARC|ISO)-\d{2}$")
    claim: str = Field(min_length=3, max_length=400)
    steps: list[StepModel] = Field(min_length=1, max_length=25)
    checks: list[CheckModel] = Field(default_factory=list, max_length=15)


# --------------------------------------------------------------------------- static validation


def _placeholders(value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield from _PLACEHOLDER.findall(value)
    elif isinstance(value, list):
        for item in value:
            yield from _placeholders(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _placeholders(item)


def validate_case(
    case: CaseModel,
    spec: Spec,
    *,
    operations: Iterable[str],
    callers: Iterable[str],
    rules: Iterable[str],
) -> list[str]:
    """Everything that can be checked before the case is ever replayed."""
    problems: list[str] = []
    allowed_ops, allowed_callers = set(operations), set(callers)
    if case.rule_id not in set(rules):
        problems.append(f"rule {case.rule_id} is not in the scope of this task")
    seen: set[str] = set()
    for step in case.steps:
        if step.id in seen:
            problems.append(f"duplicate step id {step.id!r}")
        if step.operation_id not in allowed_ops:
            problems.append(f"step {step.id}: operation {step.operation_id!r} is not allowed here")
        elif set(step.path_params) != set(spec.operations[step.operation_id].path_params):
            wanted = list(spec.operations[step.operation_id].path_params)
            problems.append(f"step {step.id}: path_params must be exactly {wanted}")
        if step.caller not in allowed_callers or step.caller not in CALLERS:
            problems.append(f"step {step.id}: caller {step.caller!r} is not allowed here")
        for source, _path in _placeholders([step.path_params, step.query, step.body]):
            if source not in seen:
                problems.append(
                    f"step {step.id}: refers to {source!r}, which is not an earlier step"
                )
        seen.add(step.id)
    all_steps = {step.id for step in case.steps}
    for index, check in enumerate(case.checks):
        try:
            node = expr.parse(check.expr)
        except expr.ExprError as exc:
            problems.append(f"check {index}: {exc}")
            continue
        if node[0] != "cmp":
            problems.append(f"check {index}: a check must be one comparison such as a == b")
        for source in sorted(expr.references(node) - all_steps):
            problems.append(f"check {index}: refers to unknown step {source!r}")
    has_expectation = any(s.expect_status or s.expect_error_code for s in case.steps) or case.checks
    if not has_expectation:
        problems.append(
            "the case states no expectation (expect_status, expect_error_code or checks)"
        )
    return problems


# --------------------------------------------------------------------------- values


def lookup(results: dict[str, dict[str, Any]], step: str, path: str) -> Any:
    if step not in results:
        raise CaseError(f"step {step!r} has not run")
    current: Any = results[step]
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise CaseError(f"{{{{{step}.{path}}}}} does not exist in the answer")
    return current


def resolve(value: Any, results: dict[str, dict[str, Any]]) -> Any:
    """Replace placeholders; a string that is exactly one placeholder keeps the referenced type."""
    if isinstance(value, str):
        whole = _WHOLE.match(value)
        if whole:
            return lookup(results, whole.group(1), whole.group(2))
        return _PLACEHOLDER.sub(lambda m: str(lookup(results, m.group(1), m.group(2))), value)
    if isinstance(value, list):
        return [resolve(item, results) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, results) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------- replay


@dataclass(frozen=True)
class CaseVerdict:
    case_id: str
    verdict: Literal["passed", "failed", "error"]
    detail: str = ""
    evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def replay_case(case: CaseModel, http: TargetHttp) -> CaseVerdict:
    """Run the case on a target and say whether the service behaved as the rule requires."""
    results: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []

    def done(verdict: Literal["passed", "failed", "error"], detail: str = "") -> CaseVerdict:
        return CaseVerdict(case.case_id, verdict, detail, tuple(evidence))

    for step in case.steps:
        try:
            path_params = {k: str(resolve(v, results)) for k, v in step.path_params.items()}
            query = {k: str(resolve(v, results)) for k, v in step.query.items()}
            body = resolve(step.body, results)
            answer = http.call(step.operation_id, step.caller, path_params, query, body)
        except (CaseError, CallError) as exc:
            return done("error", f"step {step.id}: {exc}")
        except httpx.HTTPError as exc:
            return done("error", f"step {step.id}: {type(exc).__name__}")
        envelope = answer.body if isinstance(answer.body, dict) else {}
        results[step.id] = {**envelope, "status": answer.status}
        shown = (
            json.dumps(answer.body, ensure_ascii=False)
            if answer.body is not None
            else answer.raw_text
        )
        evidence.append(
            {
                "step": step.id,
                "operation_id": step.operation_id,
                "caller": step.caller,
                "status": answer.status,
                "error_code": answer.error_code,
                "body": shown[:1500],
            }
        )
        if step.expect_status is not None and answer.status != step.expect_status:
            got = f"{answer.status} {answer.error_code or ''}".strip()
            return done(
                "failed", f"step {step.id}: expected HTTP {step.expect_status}, got HTTP {got}"
            )
        if step.expect_error_code is not None and answer.error_code != step.expect_error_code:
            return done(
                "failed",
                f"step {step.id}: expected error {step.expect_error_code}, got {answer.error_code}",
            )

    def look(step_id: str, path: str) -> Any:
        try:
            return lookup(results, step_id, path)
        except CaseError as exc:
            raise expr.MissingValue(str(exc)) from exc

    for index, check in enumerate(case.checks):
        try:
            holds = expr.evaluate(check.expr, look)
        except expr.MissingValue as exc:
            # The service did not deliver something the rule requires: that is a failed case.
            return done("failed", f"check {index}: {exc}")
        except expr.ExprError as exc:
            return done("error", f"check {index}: {exc}")
        if not isinstance(holds, bool):
            return done("error", f"check {index}: the expression is not a comparison")
        if not holds:
            note = f" ({check.message})" if check.message else ""
            return done("failed", f"check {index}: {check.expr} does not hold{note}")
    return done("passed")
