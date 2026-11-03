"""Single-process local assets. Review decisions never authorize execution in F1."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api_test_agent.tasks.models import (
    CaseRevision,
    Contract,
    EvidenceRecord,
    ExecutionRecord,
    Issue,
    ReviewDecision,
    Task,
    Transition,
)

TRANSITIONS = {
    "created": {"discovered", "blocked", "aborted"},
    "discovered": {"designed", "blocked", "aborted"},
    "designed": {"awaiting_review", "blocked", "aborted"},
    "awaiting_review": {"designed", "ready", "blocked", "aborted"},
    "ready": {"awaiting_review", "running", "blocked", "aborted"},
    "running": {"completed", "incomplete", "blocked", "aborted"},
    "incomplete": {"running", "completed", "ready", "blocked", "aborted"},
    "blocked": {"created", "ready", "running", "aborted"},
    "completed": {"ready", "running", "blocked", "aborted"},
    "aborted": set(),
}


def _write_new(path: Path, value: Contract) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(value.model_dump_json(indent=2) + "\n")


def _replace_task(path: Path, task: Task) -> None:
    # Keep the previous complete file until the new one is fully written.
    fd, name = tempfile.mkstemp(prefix="task-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(task.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class TaskStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _folder(self, task_id: str) -> Path:
        Task(task_id=task_id)  # Same strict validation for every read and write path.
        folder = self.root / task_id
        if not folder.resolve().is_relative_to(self.root):
            raise ValueError("task directory escapes store")
        return folder

    def create(self, task: Task) -> Path:
        if task.status != "created" or task.history:
            raise ValueError("new tasks must start at created with empty history")
        folder = self._folder(task.task_id)
        folder.mkdir(parents=True, exist_ok=False)
        _write_new(folder / "task.json", task)
        return folder

    def load(self, task_id: str) -> Task:
        task = Task.model_validate_json((self._folder(task_id) / "task.json").read_text("utf-8"))
        if task.task_id != task_id:
            raise ValueError("task identity differs from directory")
        return task

    def list_tasks(self) -> list[Task]:
        if not self.root.is_dir():
            return []
        tasks: list[Task] = []
        for task_dir in sorted(self.root.iterdir()):
            if task_dir.is_dir() and (task_dir / "task.json").is_file():
                try:
                    tasks.append(self.load(task_dir.name))
                except Exception:
                    continue
        return tasks

    def configure(self, task_id: str, **updates: Any) -> Task:
        task = self.load(task_id)
        allowed = {
            "target_project",
            "target_commit",
            "scope",
            "repository",
            "source_paths",
            "document_paths",
            "environment",
            "adapter",
            "version_manifest",
        }
        if not updates.keys() <= allowed:
            raise ValueError("only target settings can be configured")
        # Changing a target after design would invalidate every binding. Use a new task.
        if (
            task.status != "created"
            or self.cases(task_id)
            or (self._folder(task_id) / "inventory.json").exists()
        ):
            raise ValueError("target is locked after assets or discovery; create a new task")
        configured = Task.model_validate(task.model_dump() | updates)
        _replace_task(self._folder(task_id) / "task.json", configured)
        return configured

    def transition(self, task_id: str, status: str, reason: str) -> Task:
        task = self.load(task_id)
        if status not in TRANSITIONS.get(task.status, set()):
            raise ValueError(f"unsupported task transition: {task.status} -> {status}")
        if status in {"discovered", "designed", "awaiting_review"}:
            task.require_target()
        if status in {"ready", "running"}:
            from api_test_agent.tasks.approval import check_approval, read_approval

            pointer_file = self._folder(task_id) / "active-approval.json"
            if not pointer_file.is_file():
                raise ValueError(f"{status} requires an active approval snapshot")
            pointer = json.loads(pointer_file.read_text("utf-8"))
            snapshot = read_approval(self, task_id, pointer["approval_id"])
            check_approval(
                self,
                task_id,
                pointer["approval_id"],
                snapshot.execution_config,
                require_ready=False,
            )
        if status in {"completed", "incomplete"}:
            pointer_file = self._folder(task_id) / "active-execution.json"
            if not pointer_file.is_file():
                raise ValueError(f"{status} requires an active execution run")
        if status == "discovered":
            from api_test_agent.tasks.discovery import Inventory

            path = self._folder(task_id) / "inventory.json"
            if not path.is_file():
                raise ValueError("discovered requires a saved inventory")
            inventory = Inventory.model_validate_json(path.read_text("utf-8"))
            if inventory.target_commit != task.target_commit:
                raise ValueError("inventory target differs from task")
        if status in {"designed", "awaiting_review"} and not self.cases(task_id):
            raise ValueError("design requires case revisions")
        event = Transition(
            previous=task.status, current=status, reason=reason, at=datetime.now(UTC)
        )
        updated = Task.model_validate(
            task.model_dump() | {"status": status, "history": [*task.history, event]}
        )
        _replace_task(self._folder(task_id) / "task.json", updated)
        return updated

    def cases(self, task_id: str) -> list[CaseRevision]:
        self.load(task_id)
        return [
            CaseRevision.model_validate_json(p.read_text("utf-8"))
            for p in sorted((self._folder(task_id) / "cases").glob("*/*.json"))
        ]

    def add_case(self, task_id: str, case: CaseRevision) -> Path:
        task = self.load(task_id)
        if task.status in {"running", "completed", "incomplete", "aborted"}:
            raise ValueError("task no longer accepts design revisions")
        if task.target_commit != case.target_commit:
            raise ValueError("select a matching task commit before importing cases")
        folder = self._folder(task_id) / "cases" / case.case_id
        revisions = sorted(folder.glob("*.json")) if folder.exists() else []
        if case.revision != len(revisions) + 1:
            raise ValueError("revision must follow the latest stored revision")
        if revisions:
            previous = CaseRevision.model_validate_json(revisions[-1].read_text("utf-8"))
            if case.supersedes != previous.fingerprint():
                raise ValueError("supersedes must bind the previous revision")
        elif case.supersedes is not None:
            raise ValueError("first revision cannot supersede another")
        # A newly imported hash has no review decisions: its effective state is always draft.
        path = folder / f"{case.revision:08d}.json"
        _write_new(path, case)
        if task.status == "ready":
            self.transition(
                task_id, "awaiting_review", "New case revision invalidates execution approval"
            )
        return path

    def review_history(self, task_id: str) -> list[ReviewDecision]:
        self.load(task_id)
        folder = self._folder(task_id) / "reviews"
        return [
            ReviewDecision.model_validate_json(p.read_text("utf-8"))
            for p in sorted(folder.glob("*.json"))
        ]

    def record_review(self, decision: ReviewDecision) -> Path:
        cases = self.cases(decision.task_id)
        latest = {case.case_id: case for case in cases}
        hashes = {case.fingerprint() for case in latest.values()}
        if decision.revision_hash not in hashes:
            raise ValueError("review must reference an existing latest revision")
        if decision.decision == "draft":
            raise ValueError("cannot reset review history")
        if decision.decision == "approved":
            from api_test_agent.tasks.approval import read_approval

            if decision.approval_id is None:
                raise ValueError("approved requires a frozen host approval snapshot")
            snapshot = read_approval(self, decision.task_id, decision.approval_id)
            matching = next(
                (
                    d
                    for d in snapshot.submission.decisions
                    if d.revision_hash == decision.revision_hash and d.decision == "approved"
                ),
                None,
            )
            if (
                matching is None
                or snapshot.actor != decision.actor
                or matching.reason != decision.reason
            ):
                raise ValueError("review decision does not match frozen human submission")
        history = self.review_history(decision.task_id)
        previous = next(
            (r.decision for r in reversed(history) if r.revision_hash == decision.revision_hash),
            "draft",
        )
        allowed = {
            "draft": {"pending_review"},
            "pending_review": {"approved", "rejected", "deferred"},
            "approved": {"approved", "pending_review", "rejected", "deferred"},
            "rejected": {"pending_review"},
            "deferred": {"pending_review"},
        }
        if decision.decision not in allowed.get(previous, set()):
            raise ValueError(f"unsupported review transition: {previous} -> {decision.decision}")
        path = self._folder(decision.task_id) / "reviews" / f"{len(history) + 1:08d}.json"
        _write_new(path, decision)
        if decision.decision != "approved" and self.load(decision.task_id).status == "ready":
            self.transition(decision.task_id, "awaiting_review", "Human review reopened")
        return path

    def review_states(self, task_id: str) -> dict[str, str]:
        latest = {case.case_id: case for case in self.cases(task_id)}
        history = self.review_history(task_id)
        return {
            case.case_id: next(
                (r.decision for r in reversed(history) if r.revision_hash == case.fingerprint()),
                "draft",
            )
            for case in latest.values()
        }

    def add_execution_record(
        self, task_id: str, execution_id: str, record: ExecutionRecord
    ) -> Path:
        folder = self._folder(task_id) / "executions" / execution_id / "records"
        folder.mkdir(parents=True, exist_ok=True)
        # Use a combination of case hash or index to store immutable record
        path = folder / f"{record.revision_hash[:16]}.json"
        _write_new(path, record)
        return path

    def execution_records(
        self, task_id: str, execution_id: str | None = None
    ) -> list[ExecutionRecord]:
        if execution_id is None:
            active_file = self._folder(task_id) / "active-execution.json"
            if not active_file.is_file():
                return []
            execution_id = json.loads(active_file.read_text("utf-8")).get("execution_id")
            if not execution_id:
                return []
        folder = self._folder(task_id) / "executions" / execution_id / "records"
        if not folder.is_dir():
            return []
        return [
            ExecutionRecord.model_validate_json(p.read_text("utf-8"))
            for p in sorted(folder.glob("*.json"))
        ]

    def save_evidence(self, task_id: str, execution_id: str, evidence: EvidenceRecord) -> Path:
        folder = self._folder(task_id) / "executions" / execution_id / "evidence"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{evidence.evidence_id}.json"
        _write_new(path, evidence)
        return path

    def load_evidence(
        self, task_id: str, evidence_id: str, execution_id: str | None = None
    ) -> EvidenceRecord | None:
        if execution_id is not None:
            path = (
                self._folder(task_id)
                / "executions"
                / execution_id
                / "evidence"
                / f"{evidence_id}.json"
            )
            if path.is_file():
                return EvidenceRecord.model_validate_json(path.read_text("utf-8"))
            return None
        # Search all executions
        for path in self._folder(task_id).glob(f"executions/*/evidence/{evidence_id}.json"):
            return EvidenceRecord.model_validate_json(path.read_text("utf-8"))
        return None

    def save_issue(self, task_id: str, execution_id: str, issue: Issue) -> Path:
        folder = self._folder(task_id) / "executions" / execution_id / "issues"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{issue.issue_id}.json"
        _write_new(path, issue)
        return path

    def issues(self, task_id: str, execution_id: str | None = None) -> list[Issue]:
        if execution_id is None:
            active_file = self._folder(task_id) / "active-execution.json"
            if not active_file.is_file():
                return []
            execution_id = json.loads(active_file.read_text("utf-8")).get("execution_id")
            if not execution_id:
                return []
        folder = self._folder(task_id) / "executions" / execution_id / "issues"
        if not folder.is_dir():
            return []
        return [
            Issue.model_validate_json(p.read_text("utf-8")) for p in sorted(folder.glob("*.json"))
        ]
