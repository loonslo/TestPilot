from copy import deepcopy

import pytest
from pydantic import ValidationError

from api_test_agent.cli import main
from api_test_agent.tasks.models import CaseRevision, ExecutionRecord, Task
from api_test_agent.tasks.store import TaskStore

COMMIT = "a" * 40


def sample(origin="human"):
    return dict(
        case_id="case-one",
        revision=1,
        title="Synthetic example",
        origin=origin,
        target_commit=COMMIT,
        operation_ids=["list"],
        purpose="Check response",
        scenario="normal",
        rules=[
            dict(
                rule_id="r1",
                statement="Return list",
                basis="requirement",
                sources=[
                    dict(
                        source_id="s1",
                        version=COMMIT,
                        path="docs/spec.md",
                        locator="section 1",
                        content_hash="b" * 64,
                        summary="Synthetic specification",
                    )
                ],
            )
        ],
        steps=[
            dict(
                step_id="request",
                phase="test",
                operation_id="list",
                account="synthetic",
                side_effect=False,
                parameters=[
                    dict(
                        name="limit",
                        location="query",
                        mode="literal",
                        value=1,
                        role="background",
                        reason="Valid baseline",
                        source_ids=["s1"],
                    )
                ],
            )
        ],
        assertions=[dict(expression="status == 200", expected=200, rule_ids=["r1"])],
        change_summary="Initial design",
    )


def test_empty_task_and_shared_origin_store(tmp_path):
    store = TaskStore(tmp_path)
    store.create(
        Task(task_id="demo", target_project="synthetic", target_commit=COMMIT, scope=["GET /items"])
    )
    assert store.load("demo").status == "created"
    assert not (tmp_path / "demo" / "cases").exists()
    first = CaseRevision(**sample())
    path = store.add_case("demo", first)
    before = path.read_bytes()
    second = sample("ai")
    second.update(revision=2, supersedes=first.fingerprint(), change_summary="AI proposal")
    store.add_case("demo", CaseRevision(**second))
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        store.add_case("demo", first)
    with pytest.raises(FileExistsError):
        store.create(store.load("demo"))


@pytest.mark.parametrize("change", ["reason", "expected", "commit", "source", "cleanup"])
def test_full_definition_hash(change):
    original = sample()
    changed = deepcopy(original)
    if change == "reason":
        changed["steps"][0]["parameters"][0]["reason"] = "Different reason"
    elif change == "expected":
        changed["assertions"][0]["expected"] = 201
    elif change == "commit":
        changed["target_commit"] = "c" * 40
    elif change == "source":
        changed["rules"][0]["sources"][0]["summary"] = "Changed specification"
    else:
        changed["steps"].append(
            dict(
                step_id="cleanup",
                phase="cleanup",
                operation_id="list",
                account="synthetic",
                side_effect=True,
            )
        )
    assert CaseRevision(**original).fingerprint() != CaseRevision(**changed).fingerprint()


@pytest.mark.parametrize("change", ["source", "reason", "dependency", "result", "path"])
def test_invalid_definition_rejected(change):
    data = sample()
    if change == "source":
        data["steps"][0]["parameters"][0]["source_ids"] = ["unknown"]
    elif change == "reason":
        data["steps"][0]["parameters"][0]["reason"] = " "
    elif change == "dependency":
        data["steps"][0]["depends_on"] = ["later"]
    elif change == "result":
        data["outcome"] = "passed"
    else:
        data["rules"][0]["sources"][0]["path"] = "../private"
    with pytest.raises(ValidationError):
        CaseRevision(**data)


def test_missing_execution_evidence_rejected():
    with pytest.raises(ValidationError):
        ExecutionRecord(
            task_id="demo",
            revision_hash="b" * 64,
            attempt=1,
            outcome="passed",
            reason="Model says passed",
        )


def test_cli_without_legacy_config(tmp_path, capsys):
    prefix = ["task", "--root", str(tmp_path)]
    assert (
        main(
            [
                *prefix,
                "create",
                "demo",
                "--project",
                "synthetic",
                "--commit",
                COMMIT,
                "--scope",
                "GET /items",
            ]
        )
        == 0
    )
    assert main([*prefix, "show", "demo"]) == 0
    assert '"status": "created"' in capsys.readouterr().out
    assert main([*prefix, "show", "../escape"]) == 2


def test_draft_selects_target_per_task(tmp_path):
    store = TaskStore(tmp_path)
    store.create(Task(task_id="draft"))
    with pytest.raises(ValueError, match="select project"):
        store.load("draft").require_target()
    configured = store.configure(
        "draft",
        target_project="chosen-by-user",
        target_commit=COMMIT,
        scope=["GET /items"],
        document_paths=["docs/api.json"],
    )
    configured.require_target()
    store.add_case("draft", CaseRevision(**sample()))
    with pytest.raises(ValueError, match="locked"):
        store.configure("draft", target_commit="b" * 40)
    assert store.load("draft").target_project == "chosen-by-user"
    store.create(Task(task_id="other"))
    assert store.load("other").target_project is None


def test_task_transitions_persist_and_cannot_claim_completion(tmp_path):
    store = TaskStore(tmp_path)
    store.create(Task(task_id="draft"))
    with pytest.raises(ValueError):
        store.transition("draft", "completed", "Fabricated success")
    blocked = store.transition("draft", "blocked", "Waiting for task selection")
    assert blocked.history[0].previous == "created"
    assert blocked.history[0].current == "blocked"
    assert store.load("draft") == blocked
    resumed = store.transition("draft", "created", "Resume preparation")
    assert len(resumed.history) == 2
    assert resumed.history[0] == blocked.history[0]
    with pytest.raises(ValueError):
        store.transition("draft", "blocked", " ")
    assert store.load("draft") == resumed
    store.transition("draft", "aborted", "Cancelled")
    with pytest.raises(ValueError):
        store.transition("draft", "created", "Restart aborted task")


def test_review_bound_to_revision_and_new_revision_starts_draft(tmp_path):
    from datetime import UTC, datetime

    from api_test_agent.tasks.models import ReviewDecision

    store = TaskStore(tmp_path)
    store.create(Task(task_id="demo", target_commit=COMMIT))
    first = CaseRevision(**sample())
    store.add_case("demo", first)
    assert store.review_states("demo") == {"case-one": "draft"}

    def decision(state, fingerprint=None):
        return ReviewDecision(
            task_id="demo",
            revision_hash=fingerprint or first.fingerprint(),
            decision=state,
            actor="local-user",
            at=datetime.now(UTC),
            reason="Review feedback",
        )

    with pytest.raises(ValueError):
        store.record_review(decision("approved"))
    with pytest.raises(ValueError):
        store.record_review(decision("rejected"))
    store.record_review(decision("pending_review"))
    store.record_review(decision("deferred"))
    assert store.review_states("demo") == {"case-one": "deferred"}
    second = sample("ai")
    second.update(revision=2, supersedes=first.fingerprint())
    store.add_case("demo", CaseRevision(**second))
    assert store.review_states("demo") == {"case-one": "draft"}
    assert len(store.review_history("demo")) == 2
    with pytest.raises(ValueError):
        store.record_review(decision("pending_review"))


def test_cli_draft_then_configure_without_reading_target(tmp_path, capsys):
    args = ["task", "--root", str(tmp_path)]
    assert main([*args, "create", "draft"]) == 0
    assert main([*args, "validate-target", "draft"]) == 2
    assert (
        main(
            [
                *args,
                "configure",
                "draft",
                "--project",
                "user-choice",
                "--commit",
                COMMIT,
                "--scope",
                "GET /items",
                "--document-paths",
                "docs/not-created.json",
            ]
        )
        == 0
    )
    assert main([*args, "validate-target", "draft"]) == 0
    assert "not yet verified" in capsys.readouterr().out
    assert main([*args, "state", "draft", "blocked", "--reason", "Awaiting materials"]) == 0


@pytest.mark.parametrize("path", ["../escape", ".env", "src/.auth"])
def test_target_read_scope_rejects_invalid_paths(path):
    with pytest.raises(ValidationError):
        Task(task_id="demo", document_paths=[path])


def test_document_paths_accept_absolute_files_outside_workspace():
    task = Task(task_id="demo", document_paths=["D:/AppleProject/api-docs/swagger.json"])
    assert task.document_paths == ("D:/AppleProject/api-docs/swagger.json",)


def test_operation_keeps_uncertainty_and_provenance():
    from api_test_agent.tasks.models import Operation

    source = sample()["rules"][0]["sources"][0]
    operation = Operation(
        operation_id="list",
        method="GET",
        path="/v1/items",
        original_operation_ids=["get_items"],
        mount_prefix="/v1",
        status="conflict",
        reason="Document and code disagree",
        sources=[source],
    )
    assert operation.key == "GET /v1/items"
    assert operation.status == "conflict"
    with pytest.raises(ValidationError):
        Operation(**(operation.model_dump() | {"reason": ""}))
