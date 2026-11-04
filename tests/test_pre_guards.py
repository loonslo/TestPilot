"""Tests for execution pre-guards.

Covers environment, execution_kind, paid budget, version probe, and UI boundary.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_task_design import prepared as prepared
from test_task_execution import approved_setup as approved_setup

from api_test_agent.regression.cli import cmd_plan
from api_test_agent.tasks.approval import ExecutionConfig, read_approval
from api_test_agent.tasks.design_model import live_generator
from api_test_agent.tasks.execution import execute_task
from api_test_agent.tasks.models import Task
from api_test_agent.tasks.store import TaskStore
from api_test_agent.ui.server import make_server, validate_task_id


def test_environment_guard_rejects_production() -> None:
    for forbidden in ["prod", "production", "prod-cluster", "online-prod"]:
        with pytest.raises(ValueError, match="production environment .* is strictly forbidden"):
            ExecutionConfig(
                environment=forbidden,
                target_commit="a" * 40,
                base_url="http://127.0.0.1:18080",
                allowed_operations=("op_test",),
            )


def test_environment_guard_rejects_mismatched_task_env(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    task = Task(task_id="env-task", environment="dev_sandbox", target_commit="a" * 40)
    store.create(task)

    folder = tmp_path / "env-task"
    (folder / "active-approval.json").write_text(
        json.dumps({"approval_id": "app1"}), encoding="utf-8"
    )

    config = ExecutionConfig(
        environment="staging_sandbox",
        target_commit="a" * 40,
        base_url="http://127.0.0.1:18080",
        allowed_operations=("op_test",),
    )

    with pytest.raises(ValueError, match="cannot be executed|does not match"):
        execute_task(store, "env-task", approval_id="app1", config=config)


def test_unsupported_execution_kind_guard(approved_setup) -> None:
    store, config, approval_id, _ = approved_setup

    cases = list(store.cases("demo"))
    assert len(cases) >= 2
    case_junit = cases[0].model_copy(update={"execution_kind": "junit"})
    folder = store.folder("demo") / "cases" / case_junit.case_id
    (folder / "0001.json").write_text(case_junit.model_dump_json(), encoding="utf-8")

    snapshot = read_approval(store, "demo", approval_id)
    new_cases = (case_junit, snapshot.cases[1])
    new_snapshot = snapshot.model_copy(update={"cases": new_cases})
    folder = store.folder("demo") / "approvals" / approval_id
    (folder / "snapshot.json").write_text(new_snapshot.model_dump_json(), encoding="utf-8")

    mock_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"items": []}))
    client = httpx.Client(transport=mock_transport)

    with (
        patch("api_test_agent.tasks.execution.read_approval", return_value=new_snapshot),
        patch("api_test_agent.tasks.execution.check_approval"),
        patch("api_test_agent.tasks.approval.check_approval"),
    ):
        execute_task(store, "demo", approval_id=approval_id, config=config, http_client=client)

    records = store.execution_records("demo")
    rec_kinds = {r.revision_hash: r.outcome for r in records}
    assert rec_kinds[case_junit.fingerprint()] == "blocked"
    assert rec_kinds[snapshot.cases[1].fingerprint()] == "passed"

    issues = store.issues("demo")
    assert any("Unsupported execution_kind" in i.title for i in issues)


def test_paid_budget_guard_zero_budget_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "model.toml"
    config_file.write_text(
        """
[model]
model_id = "deepseek-chat"
base_url = "https://api.deepseek.com"
api_key_env = "DUMMY_KEY"

[budget]
max_cost_usd = 0.0
""",
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"DUMMY_KEY": "sk-dummy"}):
        gen = live_generator(config_file)
        with pytest.raises(ValueError, match="requires an explicit positive budget cap"):
            gen("generate cases")


def test_paid_budget_guard_worst_case_cost_reservation(tmp_path: Path) -> None:
    config_file = tmp_path / "model_tight.toml"
    config_file.write_text(
        """
[model]
model_id = "deepseek-chat"
base_url = "https://api.deepseek.com"
api_key_env = "DUMMY_KEY"
max_output_tokens = 4096

[budget]
max_cost_usd = 0.00001
""",
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"DUMMY_KEY": "sk-dummy"}):
        gen = live_generator(config_file)
        with pytest.raises(ValueError, match="Pre-call budget reservation exceeded"):
            gen("generate cases with very long text " * 50)


def test_paid_budget_guard_unknown_model_pricing_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "model_unknown.toml"
    config_file.write_text(
        """
[model]
model_id = "unknown-corporate-model-v9"
base_url = "https://corporate.ai/api/v1"
api_key_env = "DUMMY_KEY"

[budget]
max_cost_usd = 0.05
""",
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"DUMMY_KEY": "sk-dummy"}):
        gen = live_generator(config_file)
        with pytest.raises(ValueError, match="unknown pricing for external commercial model"):
            gen("generate cases")


def test_paid_budget_guard_regression_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESTPILOT_ALLOW_PAID_MODEL", raising=False)

    args = MagicMock()
    args.stub = False
    args.no_ai = False
    args.allow_paid = False
    args.config = None

    mock_project = MagicMock()
    mock_project.config.model = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("api_test_agent.regression.cli._project", lambda _: mock_project)
        code = cmd_plan(args)
        assert code != 0


def test_running_service_version_verification(approved_setup) -> None:
    store, config, approval_id, _ = approved_setup

    matching_commit = config.target_commit
    mismatched_commit = "f" * 40

    def mock_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health/ready":
            return httpx.Response(200, json={"commit": matching_commit})
        return httpx.Response(200, json={"items": [{"id": 1}]})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    cfg_with_version = config.model_copy(
        update={
            "version_endpoint": "/health/ready",
            "require_version_verification": True,
        }
    )
    folder = store.folder("demo") / "approvals" / approval_id
    snapshot = read_approval(store, "demo", approval_id)
    new_snapshot = snapshot.model_copy(update={"execution_config": cfg_with_version})
    (folder / "snapshot.json").write_text(new_snapshot.model_dump_json(), encoding="utf-8")

    # 1. Matching commit verifies successfully
    with patch("api_test_agent.tasks.execution.check_approval"):
        execute_task(
            store, "demo", approval_id=approval_id, config=cfg_with_version, http_client=client
        )
    exec_folder = store.folder("demo") / "executions"
    latest_exec = sorted(exec_folder.glob("*/execution.json"))[-1]
    summary = json.loads(latest_exec.read_text("utf-8"))
    assert summary["version_verified"] is True
    assert summary["actual_commit"] == matching_commit
    assert summary["declared_commit"] == matching_commit

    # 2. Mismatched commit with require_version_verification raises ValueError
    task_file = store.folder("demo") / "task.json"
    t_data = json.loads(task_file.read_text("utf-8"))
    t_data["status"] = "ready"
    task_file.write_text(json.dumps(t_data), encoding="utf-8")

    def mock_mismatched(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health/ready":
            return httpx.Response(200, json={"commit": mismatched_commit})
        return httpx.Response(200, json={"items": []})

    client_mismatch = httpx.Client(transport=httpx.MockTransport(mock_mismatched))
    with (
        patch("api_test_agent.tasks.execution.check_approval"),
        pytest.raises(
            ValueError, match="Version verification failed: running service probed commit"
        ),
    ):
        execute_task(
            store,
            "demo",
            approval_id=approval_id,
            config=cfg_with_version,
            http_client=client_mismatch,
        )


def test_ui_access_boundary_task_id_and_execution_guard(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid or unsafe task_id"):
        validate_task_id("../etc/passwd")

    with pytest.raises(ValueError, match="Invalid or unsafe task_id"):
        validate_task_id("task/invalid")

    assert validate_task_id("valid-task_01.dev") is None

    server = make_server(tmp_path, host="127.0.0.1", port=19876)

    handler_cls = server.RequestHandlerClass
    handler = handler_cls.__new__(handler_cls)
    handler.server = server
    handler.headers = {"Host": "127.0.0.1:19876", "Content-Length": "0"}
    handler.store = TaskStore(tmp_path)
    handler.path = "/api/tasks/my-task/execute"

    errors: list[tuple[str, int]] = []
    handler._send_error = lambda msg, status=HTTPStatus.BAD_REQUEST: errors.append((msg, status))

    handler.rfile = MagicMock()
    handler.rfile.read.return_value = b"{}"

    handler.do_POST()
    assert len(errors) == 1
    msg, status = errors[0]
    assert status == HTTPStatus.FORBIDDEN
    assert "UI execution guard rejected" in msg
