"""Validated F1 contracts. Definitions never contain authoritative execution results."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceRef(Contract):
    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    path: str
    locator: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or value == "."
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or ":" in value
        ):
            raise ValueError("source path must be workspace-relative POSIX path")
        return value


class Rule(Contract):
    rule_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    basis: Literal["requirement", "implementation", "hypothesis"]
    sources: tuple[SourceRef, ...] = Field(min_length=1)
    confirmed_on: str | None = None


class Parameter(Contract):
    name: str = Field(min_length=1)
    location: Literal["path", "query", "header", "body"]
    mode: Literal["literal", "reference", "generated", "omitted"]
    value: Any = None
    role: Literal["target", "background"]
    reason: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def explicit_value(self) -> Parameter:
        if self.mode == "literal" and "value" not in self.model_fields_set:
            raise ValueError(
                "literal mode requires an explicit value; use omitted for missing fields"
            )
        if self.mode == "omitted" and self.value is not None:
            raise ValueError("omitted fields cannot carry a value")
        return self


class Step(Contract):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    phase: Literal["prepare", "test", "verify", "cleanup"]
    operation_id: str = Field(min_length=1)
    account: str = Field(min_length=1)
    parameters: tuple[Parameter, ...] = ()
    depends_on: tuple[str, ...] = ()
    side_effect: bool


class Assertion(Contract):
    expression: str = Field(min_length=1)
    expected: Any
    rule_ids: tuple[str, ...] = Field(min_length=1)


class CaseRevision(Contract):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    revision: int = Field(ge=1)
    title: str = Field(min_length=1)
    origin: Literal["human", "ai"]
    target_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    operation_ids: tuple[str, ...] = Field(min_length=1)
    purpose: str = Field(min_length=1)
    scenario: Literal["normal", "validation", "boundary", "combination", "auth", "state", "repeat"]
    rules: tuple[Rule, ...] = Field(min_length=1)
    preconditions: tuple[str, ...] = ()
    execution_kind: str = Field(default="http", min_length=1)
    steps: tuple[Step, ...] = Field(min_length=1)
    assertions: tuple[Assertion, ...] = Field(min_length=1)
    change_summary: str = Field(min_length=1)
    supersedes: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def references(self) -> CaseRevision:
        rules = {rule.rule_id for rule in self.rules}
        sources = {source.source_id for rule in self.rules for source in rule.sources}
        if len(rules) != len(self.rules):
            raise ValueError("duplicate rule IDs")
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen or not set(step.depends_on) <= seen:
                raise ValueError("duplicate step or forward dependency")
            if step.operation_id not in self.operation_ids:
                raise ValueError("step operation is outside case scope")
            for parameter in step.parameters:
                if not set(parameter.source_ids) <= sources:
                    raise ValueError("unknown parameter source")
            seen.add(step.step_id)
        if not any(step.phase == "test" for step in self.steps):
            raise ValueError("a test phase is required")
        if any(not set(check.rule_ids) <= rules for check in self.assertions):
            raise ValueError("unknown assertion rule")
        if self.execution_kind == "pytest":
            raise ValueError("pytest snapshot adapter is not implemented; retain legacy assets")
        return self

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class Transition(Contract):
    previous: str
    current: str
    reason: str = Field(min_length=1)
    at: datetime


class Task(Contract):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    target_project: str | None = Field(default=None, min_length=1)
    target_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    scope: tuple[str, ...] = ()
    repository: str | None = Field(default=None, min_length=1)
    source_paths: tuple[str, ...] = ()
    document_paths: tuple[str, ...] = ()
    openapi_url: str | None = None
    environment: str | None = Field(default=None, min_length=1)
    adapter: str | None = Field(default=None, min_length=1)
    version_manifest: dict[str, str] = Field(default_factory=dict)

    @field_validator("openapi_url")
    @classmethod
    def valid_openapi_url(cls, value: str | None) -> str | None:
        from api_test_agent.tasks.openapi_import import validate_openapi_url

        return validate_openapi_url(value) if value is not None else None

    @field_validator("source_paths", "document_paths")
    @classmethod
    def allowed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            SourceRef.relative_path(value)
            if any(part.startswith(".") for part in PurePosixPath(value).parts):
                raise ValueError("hidden paths cannot be selected as task materials")
        return values

    def require_target(self) -> None:
        if (
            not self.target_project
            or not (self.target_commit or self.version_manifest)
            or not self.scope
        ):
            raise ValueError("select project, full commit and scope for this task first")
        if (
            not self.document_paths
            and not self.openapi_url
            and not (self.repository and self.source_paths)
        ):
            raise ValueError(
                "select document paths, OpenAPI URL or repository with explicit source paths"
            )

    status: Literal[
        "created",
        "discovered",
        "designed",
        "awaiting_review",
        "ready",
        "running",
        "completed",
        "incomplete",
        "blocked",
        "aborted",
    ] = "created"
    history: tuple[Transition, ...] = ()


class ReviewDecision(Contract):
    task_id: str = Field(min_length=1)
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["draft", "pending_review", "approved", "rejected", "deferred"]
    actor: str = Field(min_length=1)
    at: datetime
    reason: str = Field(min_length=1)
    approval_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class ExecutionRecord(Contract):
    task_id: str = Field(min_length=1)
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt: int = Field(ge=1)
    outcome: Literal["passed", "failed", "error", "blocked", "skipped", "not_run"]
    evidence_ids: tuple[str, ...] = ()
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_required(self) -> ExecutionRecord:
        if self.outcome in {"passed", "failed", "error"} and not self.evidence_ids:
            raise ValueError("executed outcomes require host evidence")
        return self


class Operation(Contract):
    operation_id: str = Field(min_length=1)
    method: Literal[
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "WEBSOCKET"
    ]
    path: str = Field(pattern=r"^/[^?#]*$")
    differences: tuple[str, ...] = ()
    declarations: tuple[dict[str, Any], ...] = ()
    original_operation_ids: tuple[str, ...] = ()
    mount_prefix: str = ""
    status: Literal["confirmed", "conflict", "unresolved", "out_of_scope", "unsupported"]
    reason: str = Field(min_length=1)
    sources: tuple[SourceRef, ...] = Field(min_length=1)

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


class EvidenceRecord(Contract):
    evidence_id: str = Field(pattern=r"^[0-9a-f]{16,64}$")
    task_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    operation_id: str | None = None
    phase: Literal["prepare", "test", "verify", "cleanup"]
    timestamp: datetime
    method: str = Field(min_length=1)
    url: str = Field(min_length=1)
    request_parameters: dict[str, Any] = Field(default_factory=dict)
    request_attempted: bool = False
    response_received: bool = False
    response_status: int = Field(ge=100, le=599)
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: Any = None
    duration_seconds: float = Field(default=0.0, ge=0.0)
    error: str | None = None


class StepResult(Contract):
    step_id: str = Field(min_length=1)
    phase: Literal["prepare", "test", "verify", "cleanup"]
    operation_id: str = Field(min_length=1)
    status_code: int | None = None
    evidence_id: str | None = None
    side_effect: bool = False
    side_effect_occurred: bool = False
    error: str | None = None


class AssertionResult(Contract):
    expression: str = Field(min_length=1)
    expected: Any
    actual: Any = None
    passed: bool
    rule_ids: tuple[str, ...] = ()
    error: str | None = None


class Issue(Contract):
    issue_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    task_id: str = Field(min_length=1)
    category: Literal[
        "suspected_defect",
        "case_issue",
        "environment_issue",
        "unconfirmed_hypothesis",
        "verified_defect",
        "cleanup_failure",
    ]
    title: str = Field(min_length=1)
    detail: str = ""
    case_id: str | None = None
    step_id: str | None = None
    rule_ids: tuple[str, ...] = ()
    expected: Any = None
    actual: Any = None
    evidence_ids: tuple[str, ...] = ()
    status: Literal["open", "confirmed", "dismissed"] = "open"


class OperationCoverage(Contract):
    total_discovered: int = Field(default=0, ge=0)
    confirmed_in_scope: int = Field(default=0, ge=0)
    supported: int = Field(default=0, ge=0)
    called: int = Field(default=0, ge=0)
    coverage_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class ScenarioCoverage(Contract):
    total_slots: int = Field(default=0, ge=0)
    designed: int = Field(default=0, ge=0)
    executed: int = Field(default=0, ge=0)
    coverage_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class ExecutionAccounting(Contract):
    planned: int = Field(ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    error: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    not_run: int = Field(default=0, ge=0)
    conserved: bool = True
    evidence_backed_count: int = Field(default=0, ge=0)
    missing_evidence_count: int = Field(default=0, ge=0)
    missing_evidence_ids: tuple[str, ...] = ()


class Report(Contract):
    report_id: str = Field(pattern=r"^[0-9a-f]{16,64}$")
    task_id: str = Field(min_length=1)
    generated_at: datetime
    task_status: str = Field(min_length=1)
    target_project: str | None = None
    target_commit: str | None = None
    repository: str | None = None
    environment: str | None = None
    declared_commit: str | None = None
    actual_commit: str | None = None
    version_verified: bool = False
    approval_id: str | None = None
    execution_id: str | None = None
    accounting: ExecutionAccounting
    operation_coverage: OperationCoverage
    scenario_coverage: ScenarioCoverage
    records: tuple[ExecutionRecord, ...] = ()
    issues: tuple[Issue, ...] = ()
    uncovered_operations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    missing_evidence_ids: tuple[str, ...] = ()
