"""Host-only review decisions and immutable execution approval snapshots."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from api_test_agent.tasks.design import ensure_design_complete, load_inventory, validate_cases
from api_test_agent.tasks.discovery import Inventory, task_binding
from api_test_agent.tasks.models import CaseRevision, Contract, ReviewDecision
from api_test_agent.tasks.store import TaskStore, _write_new


def fingerprint(value: Any) -> str:
    if isinstance(value, Contract):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def executor_fingerprint() -> str:
    root = Path(__file__).parent
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}
    files["expr.py"] = hashlib.sha256((root.parent / "expr.py").read_bytes()).hexdigest()
    return fingerprint(
        {
            "files": files,
            "python": list(sys.version_info[:2]),
            "httpx": importlib.metadata.version("httpx"),
            "pydantic": importlib.metadata.version("pydantic"),
        }
    )


class ExecutionConfig(Contract):
    engine: Literal["structured-http-v1"] = "structured-http-v1"
    environment: str = Field(min_length=1)
    target_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_url: str
    synthetic_only: Literal[True] = True
    # External endpoint identity cannot be proven by a user-written version label.
    version_verified: Literal[False] = False
    allowed_operations: tuple[str, ...] = Field(min_length=1)
    operation_confirmations: dict[str, str] = Field(default_factory=dict)
    accounts: dict[str, str] = Field(default_factory=dict)
    confirmed_preconditions: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    max_requests: int = Field(default=30, ge=1, le=500)
    max_response_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000)

    @field_validator("base_url")
    @classmethod
    def loopback(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme != "http"
            or parts.hostname != "127.0.0.1"
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
            or parts.port is None
            or not 1024 <= parts.port <= 65535
        ):
            raise ValueError("execution origin must be http://127.0.0.1:<explicit port>")
        return f"http://127.0.0.1:{parts.port}"

    @model_validator(mode="after")
    def account_names(self) -> ExecutionConfig:
        if len(set(self.allowed_operations)) != len(self.allowed_operations):
            raise ValueError("duplicate allowed operations")
        for alias, env_name in self.accounts.items():
            if alias == "anonymous" or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", alias):
                raise ValueError(
                    "account aliases must be names; anonymous cannot carry credentials"
                )
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
                raise ValueError("accounts contain environment variable names, never tokens")
        if any(not reason.strip() for reason in self.operation_confirmations.values()):
            raise ValueError("operation confirmations need reasons")
        return self

    @classmethod
    def load(cls, path: Path) -> ExecutionConfig:
        text = path.read_text("utf-8")
        data = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
        return cls.model_validate(data)


class CaseDecision(Contract):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected", "deferred"]
    reason: str = Field(min_length=1)


class RuleConfirmation(Contract):
    case_id: str
    rule_id: str
    statement_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_on: date
    reason: str = Field(min_length=1)


class ReviewSubmission(Contract):
    review_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1)
    decisions: tuple[CaseDecision, ...] = Field(min_length=1)
    rule_confirmations: tuple[RuleConfirmation, ...] = ()


class ApprovalSnapshot(Contract):
    approval_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    task_id: str
    task_hash: str
    inventory_hash: str
    inventory: Inventory
    review_id: str
    review_hash: str
    review_document_hash: str
    submission: ReviewSubmission
    actor: str
    at: datetime
    all_case_hashes: dict[str, str]
    cases: tuple[CaseRevision, ...] = Field(min_length=1)
    execution_config: ExecutionConfig
    executor_hash: str

    @model_validator(mode="after")
    def consistent_selection(self) -> ApprovalSnapshot:
        if (
            self.actor != self.submission.actor
            or self.review_id != self.submission.review_id
            or self.review_hash != self.submission.review_hash
        ):
            raise ValueError("approval and human submission identities differ")
        approved = {
            d.case_id: d.revision_hash
            for d in self.submission.decisions
            if d.decision == "approved"
        }
        actual = {case.case_id: case.fingerprint() for case in self.cases}
        if len(actual) != len(self.cases) or actual != approved:
            raise ValueError("frozen cases do not match the human-approved selection")
        if any(self.all_case_hashes.get(key) != value for key, value in actual.items()):
            raise ValueError("selected case differs from the reviewed revision set")
        if self.execution_config.target_commit != self.inventory.target_commit:
            raise ValueError("execution target differs from frozen inventory")
        return self


def current_cases(store: TaskStore, task_id: str) -> dict[str, CaseRevision]:
    return {case.case_id: case for case in store.cases(task_id)}


def validate_config(store: TaskStore, task_id: str, config: ExecutionConfig) -> Inventory:
    inventory = load_inventory(store, task_id)
    task = store.load(task_id)
    if config.target_commit != task.target_commit or (
        task.environment is not None and task.environment != config.environment
    ):
        raise ValueError("execution target or environment differs from the task")
    operations = {o.operation_id: o for o in inventory.operations}
    for operation_id in config.allowed_operations:
        operation = operations.get(operation_id)
        if operation is None or operation.status in {"unsupported", "out_of_scope"}:
            raise ValueError("execution config includes unknown/unsupported/out-of-scope operation")
        if operation.status != "confirmed" and not config.operation_confirmations.get(operation_id):
            raise ValueError("unresolved/conflicting operations require explicit host confirmation")
    return inventory


def validate_selected(
    cases: list[CaseRevision], inventory: Inventory, config: ExecutionConfig
) -> None:
    validate_cases(cases, inventory)
    allowed = set(config.allowed_operations)
    for case in cases:
        if not set(case.operation_ids) <= allowed:
            raise ValueError(
                "approved case contains an operation absent from the execution allowlist"
            )
        for step in case.steps:
            if step.account != "anonymous" and step.account not in config.accounts:
                raise ValueError("case account alias has no host credential binding")


def review_binding(store: TaskStore, task_id: str, config: ExecutionConfig | None) -> dict:
    if config is None:
        return {"execution_config": None, "executor_hash": None}
    validate_config(store, task_id, config)
    return {
        "execution_config": config.model_dump(mode="json"),
        "executor_hash": executor_fingerprint(),
    }


def read_approval(store: TaskStore, task_id: str, approval_id: str) -> ApprovalSnapshot:
    if not re.fullmatch(r"[0-9a-f]{32}", approval_id):
        raise ValueError("invalid approval ID")
    folder = store._folder(task_id) / "approvals" / approval_id
    snapshot = ApprovalSnapshot.model_validate_json((folder / "approval.json").read_text("utf-8"))
    if snapshot.task_id != task_id or snapshot.approval_id != approval_id:
        raise ValueError("approval identity does not match task/path")
    if fingerprint(snapshot) != (folder / "approval.sha256").read_text("ascii").strip():
        raise ValueError("approval snapshot hash differs")
    if fingerprint(snapshot.inventory) != snapshot.inventory_hash:
        raise ValueError("approval inventory snapshot differs")
    return snapshot


def check_approval(
    store: TaskStore,
    task_id: str,
    approval_id: str,
    config: ExecutionConfig,
    *,
    require_ready: bool = True,
) -> ApprovalSnapshot:
    ensure_design_complete(store, task_id)
    task = store.load(task_id)
    if require_ready and task.status != "ready":
        raise ValueError("task is not ready for approved execution")
    snapshot = read_approval(store, task_id, approval_id)
    if snapshot.task_hash != task_binding(task):
        raise ValueError("task settings changed after review")
    if snapshot.execution_config != config or snapshot.executor_hash != executor_fingerprint():
        raise ValueError("execution config or executor changed after review")
    inventory = validate_config(store, task_id, config)
    if fingerprint(inventory) != snapshot.inventory_hash:
        raise ValueError("sources or interface inventory changed after review")
    latest = current_cases(store, task_id)
    hashes = {key: case.fingerprint() for key, case in latest.items()}
    if hashes != snapshot.all_case_hashes:
        raise ValueError("case set or a complete revision changed after review")
    validate_selected(list(snapshot.cases), inventory, config)
    history = store.review_history(task_id)
    for case in snapshot.cases:
        event = next((r for r in reversed(history) if r.revision_hash == case.fingerprint()), None)
        if event is None or event.decision != "approved" or event.approval_id != approval_id:
            raise ValueError("selected revision no longer has this execution approval")
    active = json.loads((store._folder(task_id) / "active-approval.json").read_text("utf-8"))
    if active.get("approval_id") != approval_id:
        raise ValueError("approval is not the current explicitly selected execution set")
    return snapshot


def approve(
    store: TaskStore, task_id: str, submission: ReviewSubmission, config: ExecutionConfig
) -> Path:
    ensure_design_complete(store, task_id)
    task = store.load(task_id)
    if task.status not in {"awaiting_review", "ready"}:
        raise ValueError("task must have exported review materials before approval")
    folder = store._folder(task_id) / "review-materials" / submission.review_id
    manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
    if fingerprint(manifest) != submission.review_hash:
        raise ValueError("submission does not bind the exact review manifest")
    document_hash = hashlib.sha256((folder / "review.md").read_bytes()).hexdigest()
    if manifest.get("review_document_hash") != document_hash:
        raise ValueError("review document changed after export")
    inventory = validate_config(store, task_id, config)
    latest = current_cases(store, task_id)
    hashes = {key: case.fingerprint() for key, case in latest.items()}
    if (
        manifest.get("task_id") != task_id
        or manifest.get("task_hash") != task_binding(task)
        or manifest.get("cases") != hashes
        or manifest.get("inventory_hash") != fingerprint(inventory)
    ):
        raise ValueError("task, sources or revisions changed since the human review")
    if (
        manifest.get("execution_config") != config.model_dump(mode="json")
        or manifest.get("executor_hash") != executor_fingerprint()
    ):
        raise ValueError("execution config was not reviewed or has changed")
    selected_ids: set[str] = set()
    states = store.review_states(task_id)
    for decision in submission.decisions:
        if (
            decision.case_id in selected_ids
            or hashes.get(decision.case_id) != decision.revision_hash
        ):
            raise ValueError("duplicate, missing or changed case in decisions")
        if states.get(decision.case_id) not in {"pending_review", "approved"}:
            raise ValueError("case must be submitted for review before a decision")
        selected_ids.add(decision.case_id)
    selected = [latest[d.case_id] for d in submission.decisions if d.decision == "approved"]
    validate_selected(selected, inventory, config)
    confirmations: set[tuple[str, str]] = set()
    for confirmation in submission.rule_confirmations:
        key = (confirmation.case_id, confirmation.rule_id)
        case = next((c for c in selected if c.case_id == confirmation.case_id), None)
        rule = (
            next((r for r in case.rules if r.rule_id == confirmation.rule_id), None)
            if case
            else None
        )
        if (
            key in confirmations
            or rule is None
            or fingerprint(rule.statement) != confirmation.statement_hash
        ):
            raise ValueError("rule confirmation does not bind a unique approved rule statement")
        confirmations.add(key)
    # Validate everything before making any authoritative review changes.
    approval_id = uuid4().hex
    output = store._folder(task_id) / "approvals" / approval_id
    output.mkdir(parents=True)
    _write_new(output / "submission.json", submission)
    snapshot = None
    if selected:
        snapshot = ApprovalSnapshot(
            approval_id=approval_id,
            task_id=task_id,
            task_hash=task_binding(task),
            inventory_hash=fingerprint(inventory),
            inventory=inventory,
            review_id=submission.review_id,
            review_hash=submission.review_hash,
            review_document_hash=document_hash,
            submission=submission,
            actor=submission.actor,
            at=datetime.now(UTC),
            all_case_hashes=hashes,
            cases=tuple(selected),
            execution_config=config,
            executor_hash=executor_fingerprint(),
        )
        _write_new(output / "approval.json", snapshot)
        (output / "approval.sha256").write_text(fingerprint(snapshot), "ascii")
    if task.status == "ready":
        store.transition(
            task_id, "awaiting_review", "New explicit execution selection is being committed"
        )
    for decision in submission.decisions:
        store.record_review(
            ReviewDecision(
                task_id=task_id,
                revision_hash=decision.revision_hash,
                decision=decision.decision,
                actor=submission.actor,
                at=datetime.now(UTC),
                reason=decision.reason,
                approval_id=approval_id if decision.decision == "approved" else None,
            )
        )
    if snapshot:
        (store._folder(task_id) / "active-approval.json").write_text(
            json.dumps({"approval_id": approval_id}), "utf-8"
        )
        check_approval(store, task_id, approval_id, config, require_ready=False)
        store.transition(task_id, "ready", "Human-approved execution selection frozen")
    return output
