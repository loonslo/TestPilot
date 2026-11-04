import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_task_design import prepared as prepared

from api_test_agent.cli import main
from api_test_agent.tasks.approval import (
    ExecutionConfig,
    ReviewSubmission,
    approve,
    check_approval,
    fingerprint,
)
from api_test_agent.tasks.design import design, export_review
from api_test_agent.tasks.models import CaseRevision, ReviewDecision


@pytest.fixture
def reviewed(prepared):
    store, inventory, output = prepared
    design(store, "demo", supplied_response=json.dumps(output))
    config = ExecutionConfig(
        environment="synthetic",
        target_commit="a" * 40,
        base_url="http://127.0.0.1:18081",
        allowed_operations=[inventory.operations[0].operation_id],
    )
    review = export_review(store, "demo", config)
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    case = store.cases("demo")[0]
    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human-reviewer",
        decisions=[
            dict(
                case_id=case.case_id,
                revision_hash=case.fingerprint(),
                decision="approved",
                reason="Reviewed synthetic baseline",
            )
        ],
    )
    return store, config, review, submission


def test_explicit_host_approval_freezes_full_snapshot_and_zero_requests(reviewed, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx.Client, "__init__", lambda *a, **kw: pytest.fail("no HTTP before execution")
    )
    store, config, _, submission = reviewed
    folder = approve(store, "demo", submission, config)
    snapshot = check_approval(store, "demo", folder.name, config)
    assert snapshot.cases[0].fingerprint() == submission.decisions[0].revision_hash
    assert snapshot.execution_config == config
    assert snapshot.inventory.snapshots
    assert store.review_states("demo") == {"list-items": "approved"}
    assert store.load("demo").status == "ready"
    assert snapshot.execution_config.version_verified is False
    assert snapshot.submission.actor == "synthetic-human-reviewer"


@pytest.mark.parametrize(
    "change", ["parameter", "reason", "assertion", "prepare", "cleanup", "version"]
)
def test_changed_complete_revision_refused_after_approval(reviewed, change):
    store, config, _, submission = reviewed
    folder = approve(store, "demo", submission, config)
    path = store.folder("demo") / "cases/list-items/00000001.json"
    data = json.loads(path.read_text("utf-8"))
    if change == "parameter":
        data["steps"][0]["parameters"][0]["value"] = 2
    elif change == "reason":
        data["steps"][0]["parameters"][0]["reason"] = "A different design"
    elif change == "assertion":
        data["assertions"][0]["expected"] = False
    elif change in {"prepare", "cleanup"}:
        step = deepcopy(data["steps"][0])
        step.update(step_id=change, phase=change, side_effect=True)
        if change == "prepare":
            data["steps"].insert(0, step)
        else:
            data["steps"].append(step)
    else:
        data["target_commit"] = "b" * 40
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="revision|case set"):
        check_approval(store, "demo", folder.name, config)


def test_new_case_invalidates_set_and_keeps_old_approval(reviewed):
    store, config, _, submission = reviewed
    folder = approve(store, "demo", submission, config)
    before = (folder / "approval.json").read_bytes()
    original = store.cases("demo")[0]
    other = CaseRevision.model_validate(original.model_dump(mode="json") | {"case_id": "new-draft"})
    store.add_case("demo", other)
    assert store.load("demo").status == "awaiting_review"
    assert store.review_states("demo")["new-draft"] == "draft"
    with pytest.raises(ValueError, match="not ready"):
        check_approval(store, "demo", folder.name, config)
    assert (folder / "approval.json").read_bytes() == before


def test_execution_config_source_executor_and_review_changes_refused(reviewed, monkeypatch):
    from api_test_agent.tasks import approval

    store, config, review, submission = reviewed
    changed = config.model_copy(update={"timeout_seconds": 5})
    with pytest.raises(ValueError, match="config"):
        approve(store, "demo", submission, changed)
    original_text = review.read_text("utf-8")
    review.write_text(original_text + "Unreviewed edit", encoding="utf-8")
    with pytest.raises(ValueError, match="document"):
        approve(store, "demo", submission, config)
    review.write_text(original_text, encoding="utf-8")
    folder = approve(store, "demo", submission, config)
    with pytest.raises(ValueError, match="config"):
        check_approval(store, "demo", folder.name, changed)
    monkeypatch.setattr(approval, "executor_fingerprint", lambda: "changed-implementation")
    with pytest.raises(ValueError, match="executor"):
        check_approval(store, "demo", folder.name, config)


def test_review_only_without_execution_config_is_not_approval(prepared):
    store, inv, output = prepared
    design(store, "demo", supplied_response=json.dumps(output))
    review = export_review(store, "demo")
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    config = ExecutionConfig(
        environment="synthetic",
        target_commit="a" * 40,
        base_url="http://127.0.0.1:18081",
        allowed_operations=[inv.operations[0].operation_id],
    )
    case = store.cases("demo")[0]
    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human-reviewer",
        decisions=[
            dict(
                case_id=case.case_id,
                revision_hash=case.fingerprint(),
                decision="approved",
                reason="Incomplete config review",
            )
        ],
    )
    with pytest.raises(ValueError, match="config"):
        approve(store, "demo", submission, config)
    assert store.load("demo").status == "awaiting_review"


def test_partial_decisions_do_not_approve_unmentioned_cases(prepared):
    store, inv, output = prepared
    case = output["cases"][0]
    for number in range(2, 4):
        extra = deepcopy(case)
        extra["case_id"] = f"draft-{number}"
        output["cases"].append(extra)
    design(store, "demo", supplied_response=json.dumps(output))
    config = ExecutionConfig(
        environment="synthetic",
        target_commit="a" * 40,
        base_url="http://127.0.0.1:18081",
        allowed_operations=[inv.operations[0].operation_id],
    )
    review = export_review(store, "demo", config)
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    cases = {c.case_id: c for c in store.cases("demo")}
    decisions = [
        dict(
            case_id=case_id,
            revision_hash=cases[case_id].fingerprint(),
            decision=decision,
            reason="Explicit synthetic decision",
        )
        for case_id, decision in [("list-items", "approved"), ("draft-2", "rejected")]
    ]
    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human",
        decisions=decisions,
    )
    folder = approve(store, "demo", submission, config)
    snapshot = check_approval(store, "demo", folder.name, config)
    assert [c.case_id for c in snapshot.cases] == ["list-items"]
    assert store.review_states("demo") == {
        "list-items": "approved",
        "draft-2": "rejected",
        "draft-3": "pending_review",
    }


def test_approved_marker_without_snapshot_and_unreviewed_submission_refused(reviewed):
    store, config, _, submission = reviewed
    with pytest.raises(ValueError, match="snapshot"):
        store.record_review(
            ReviewDecision(
                task_id="demo",
                revision_hash=submission.decisions[0].revision_hash,
                decision="approved",
                actor="fake",
                at=datetime.now(UTC),
                reason="fabricated",
            )
        )
    changed = submission.model_copy(update={"review_hash": "f" * 64})
    with pytest.raises(ValueError, match="manifest"):
        approve(store, "demo", changed, config)
    assert store.load("demo").status == "awaiting_review"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://user:pass@127.0.0.1:8080",
        "http://127.0.0.1:8080/other",
    ],
)
def test_execution_origin_is_explicit_loopback_only(url):
    with pytest.raises(ValueError):
        ExecutionConfig(
            environment="synthetic", target_commit="a" * 40, base_url=url, allowed_operations=["op"]
        )


def test_cli_explicit_approval_and_guard(reviewed, tmp_path):
    store, config, _, submission = reviewed
    decision_path = tmp_path / "decisions.json"
    config_path = tmp_path / "execution.json"
    decision_path.write_text(submission.model_dump_json(), encoding="utf-8")
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    prefix = ["task", "--root", str(store.root)]
    assert (
        main(
            [*prefix, "approve", "demo", str(decision_path), "--execution-config", str(config_path)]
        )
        == 0
    )
    active = json.loads((store.folder("demo") / "active-approval.json").read_text("utf-8"))
    assert (
        main(
            [
                *prefix,
                "verify-approval",
                "demo",
                active["approval_id"],
                "--execution-config",
                str(config_path),
            ]
        )
        == 0
    )
