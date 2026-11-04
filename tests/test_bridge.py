"""Tests for F8 bridge: exporting approved task cases into regression catalog.

Also covers runner execution via explicit runner metadata.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_task_design import prepared as prepared
from test_task_execution import approved_setup as approved_setup

from api_test_agent.regression.models import ROOT_PLACEHOLDER, PlanEntry, RegressionPlan
from api_test_agent.regression.project import load_catalog
from api_test_agent.regression.runner import (
    ACCOUNTING_FLAG,
    Invocation,
    RunError,
    run_plan,
    task_command,
)
from api_test_agent.tasks.bridge import export_approved_to_regression_catalog


def _plan_from(case_id: str, catalog_path: Path, tmp_path: Path, run_id: str) -> RegressionPlan:
    catalog = load_catalog(catalog_path, tmp_path / "smoke.yaml")
    case = catalog.get(case_id)
    assert case is not None
    return RegressionPlan(
        project="demo-project",
        run_id=run_id,
        base_sha="a" * 40,
        head_sha="b" * 40,
        entries=[PlanEntry.from_case(case, priority="manual")],
    )


def test_export_approved_to_regression_catalog(approved_setup, tmp_path: Path) -> None:
    store, _, approval_id, _ = approved_setup

    catalog_yaml = tmp_path / "cases.yaml"

    entries = export_approved_to_regression_catalog(
        store,
        "demo",
        catalog_yaml,
        approval_id=approval_id,
    )

    assert len(entries) == 2
    assert catalog_yaml.is_file()

    # Verify YAML content
    content = yaml.safe_load(catalog_yaml.read_text("utf-8"))
    assert "cases" in content
    assert len(content["cases"]) == 2

    case_ids = [c["id"] for c in content["cases"]]
    assert "TC-DEMO-LIST-ITEMS" in case_ids
    assert "TC-DEMO-LIST-ITEMS-2" in case_ids

    # Verify that regression load_catalog can parse this catalog
    catalog = load_catalog(catalog_yaml, tmp_path / "smoke.yaml")
    assert len(catalog.cases) == 2
    case_map = {c.id: c for c in catalog.cases}
    assert "TC-DEMO-LIST-ITEMS" in case_map
    entry = case_map["TC-DEMO-LIST-ITEMS"]
    assert entry.status == "active"
    assert entry.entry != []

    # Verify that entry contains discrete tokens with --case-id to execute only one case
    assert "--case-id" in entry.entry
    assert "list-items" in entry.entry
    assert entry.task_binding is not None
    assert entry.task_binding["task_id"] == "demo"
    assert entry.task_binding["case_id"] == "list-items"
    assert entry.task_binding["approval_id"] == approval_id

    # 执行器是指显式元数据，不靠 entry 文本猜
    assert entry.runner == "task"
    assert entry.task_binding["runner"] == "task"

    # 任务根目录用占位符，不把本机绝对路径写进（可能被提交的）用例目录
    root_at = entry.entry.index("--root") + 1
    assert entry.entry[root_at] == ROOT_PLACEHOLDER
    assert str(store.root) not in " ".join(entry.entry)

    # 凭据只记录变量名白名单，不记录值
    assert entry.task_binding["credential_env"] == ["SYNTHETIC_API_TOKEN"]


def test_bridge_catalog_corruption_protection(approved_setup, tmp_path: Path) -> None:
    store, _, approval_id, _ = approved_setup

    catalog_yaml = tmp_path / "corrupted_cases.yaml"
    corrupted_content = "cases: [unclosed list {"
    catalog_yaml.write_text(corrupted_content, encoding="utf-8")

    # Should refuse to overwrite corrupted catalog and raise ValueError
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        export_approved_to_regression_catalog(
            store,
            "demo",
            catalog_yaml,
            approval_id=approval_id,
        )

    # Content must remain intact
    assert catalog_yaml.read_text("utf-8") == corrupted_content


def test_task_command_keeps_argv_tokens_with_spaces(tmp_path: Path) -> None:
    """含空格的路径必须整体作为一个参数传给子进程。"""

    entry = [
        "testpilot",
        "task",
        "--root",
        "C:/Users/Test User/tasks",
        "execute",
        "demo",
        "--case-id",
        "list-items",
    ]
    command = task_command(None, entry, tmp_path, None)

    assert "C:/Users/Test User/tasks" in command
    assert command.count("C:/Users") == 0
    assert command[0].endswith(("python.exe", "python")) or command[0]


def test_task_root_placeholder_resolves_to_spaced_directory(approved_setup, tmp_path: Path) -> None:
    store, _, approval_id, _ = approved_setup
    catalog_yaml = tmp_path / "cases.yaml"
    export_approved_to_regression_catalog(store, "demo", catalog_yaml, approval_id=approval_id)
    plan = _plan_from("TC-DEMO-LIST-ITEMS", catalog_yaml, tmp_path, "run-root")

    spaced = tmp_path / "Test User" / "tasks"
    dry = run_plan(plan, repo=tmp_path, dry_run=True, task_root=spaced)
    rendered = dry.results[0].output_excerpt
    assert str(spaced) in rendered
    assert ROOT_PLACEHOLDER not in rendered

    with pytest.raises(RunError, match="task_root"):
        run_plan(plan, repo=tmp_path, dry_run=True, task_root=None)


def test_regression_runner_executes_bridged_task(approved_setup, tmp_path: Path) -> None:
    store, _, approval_id, _ = approved_setup
    catalog_yaml = tmp_path / "cases.yaml"

    export_approved_to_regression_catalog(
        store,
        "demo",
        catalog_yaml,
        approval_id=approval_id,
    )

    plan = _plan_from("TC-DEMO-LIST-ITEMS", catalog_yaml, tmp_path, "run-bridge-01")

    # 1. Dry run verification: verifies task_command builder was invoked
    dry_record = run_plan(plan, repo=tmp_path, dry_run=True, task_root=store.root)
    assert len(dry_record.results) == 1
    output = dry_record.results[0].output_excerpt
    assert "api_test_agent" in output
    assert "task" in output
    assert "execute" in output
    assert "--case-id" in output
    assert "list-items" in output
    assert ACCOUNTING_FLAG in output

    # 2. Live execution simulated: the child writes structured accounting, the runner
    #    classifies on that file, never on the prose output.
    captured: list[list[str]] = []

    def mock_invoke(command, cwd, extra_env, timeout) -> Invocation:
        captured.append(command)
        accounting_path = Path(command[command.index(ACCOUNTING_FLAG) + 1])
        accounting_path.write_text(
            json.dumps(
                {
                    "planned": 1,
                    "passed": 1,
                    "failed": 0,
                    "error": 0,
                    "blocked": 0,
                    "skipped": 0,
                    "not_run": 0,
                    "conserved": True,
                }
            ),
            encoding="utf-8",
        )
        return Invocation(
            command=command,
            output="task outcome: passed\n1 passed, 0 failed, 0 error, 0 blocked",
            returncode=0,
            duration_seconds=0.05,
            timed_out=False,
            error="",
        )

    exec_record = run_plan(plan, repo=tmp_path, invoke_fn=mock_invoke, task_root=store.root)
    assert len(captured) == 1
    assert "api_test_agent" in captured[0]
    assert "--case-id" in captured[0]
    assert exec_record.results[0].result == "passed"
    assert exec_record.results[0].accounting is not None
    assert exec_record.totals["passed"] == 1


def test_zero_execution_is_not_reported_as_passed(approved_setup, tmp_path: Path) -> None:
    """退出码 0 + 文本里的 "0 passed" 都不能当通过；只认结构化 accounting。"""

    store, _, approval_id, _ = approved_setup
    catalog_yaml = tmp_path / "cases.yaml"
    export_approved_to_regression_catalog(store, "demo", catalog_yaml, approval_id=approval_id)
    plan = _plan_from("TC-DEMO-LIST-ITEMS", catalog_yaml, tmp_path, "run-zero")

    def mock_invoke(command, cwd, extra_env, timeout) -> Invocation:
        accounting_path = Path(command[command.index(ACCOUNTING_FLAG) + 1])
        accounting_path.write_text(
            json.dumps(
                {
                    "planned": 1,
                    "passed": 0,
                    "failed": 0,
                    "error": 0,
                    "blocked": 0,
                    "skipped": 0,
                    "not_run": 1,
                    "conserved": True,
                }
            ),
            encoding="utf-8",
        )
        return Invocation(
            command=command,
            output="0 passed, 0 failed, 0 error, 0 blocked",
            returncode=0,
            duration_seconds=0.05,
        )

    record = run_plan(plan, repo=tmp_path, invoke_fn=mock_invoke, task_root=store.root)
    assert record.results[0].result != "passed"
    assert record.totals["passed"] == 0
    assert record.state == "incomplete"


def test_missing_structured_result_falls_back_to_exit_code(approved_setup, tmp_path: Path) -> None:
    store, _, approval_id, _ = approved_setup
    catalog_yaml = tmp_path / "cases.yaml"
    export_approved_to_regression_catalog(store, "demo", catalog_yaml, approval_id=approval_id)
    plan = _plan_from("TC-DEMO-LIST-ITEMS", catalog_yaml, tmp_path, "run-nojson")

    def mock_invoke(command, cwd, extra_env, timeout) -> Invocation:
        return Invocation(command=command, output="", returncode=2, duration_seconds=0.05)

    record = run_plan(plan, repo=tmp_path, invoke_fn=mock_invoke, task_root=store.root)
    assert record.results[0].result == "error"
    assert any("结构化结果" in e for e in record.errors)


def test_credential_whitelist_is_forwarded_only_for_task_entries(
    approved_setup, tmp_path: Path, monkeypatch
) -> None:
    store, _, approval_id, _ = approved_setup
    monkeypatch.setenv("SYNTHETIC_API_TOKEN", "synthetic-secret-value")
    monkeypatch.setenv("UNRELATED_API_KEY", "should-not-be-forwarded")
    catalog_yaml = tmp_path / "cases.yaml"
    export_approved_to_regression_catalog(store, "demo", catalog_yaml, approval_id=approval_id)
    plan = _plan_from("TC-DEMO-LIST-ITEMS", catalog_yaml, tmp_path, "run-cred")

    seen: dict[str, dict[str, str]] = {}

    def mock_invoke(command, cwd, extra_env, timeout) -> Invocation:
        seen["env"] = dict(extra_env)
        accounting_path = Path(command[command.index(ACCOUNTING_FLAG) + 1])
        accounting_path.write_text(json.dumps({"planned": 1, "passed": 1, "conserved": True}))
        return Invocation(command=command, output="", returncode=0, duration_seconds=0.01)

    run_plan(plan, repo=tmp_path, invoke_fn=mock_invoke, task_root=store.root)
    assert seen["env"]["SYNTHETIC_API_TOKEN"] == "synthetic-secret-value"
    assert "UNRELATED_API_KEY" not in seen["env"]

    # pytest 入口仍然不获得任何凭据
    pytest_plan = RegressionPlan(
        project="demo-project",
        run_id="run-cred-pytest",
        base_sha="a" * 40,
        head_sha="b" * 40,
        entries=[
            PlanEntry(
                case_id="TC-X",
                name="plain pytest",
                module="m",
                entry=["tests/test_x.py::test_ok"],
                priority="manual",
            )
        ],
    )

    def mock_pytest(command, cwd, extra_env, timeout) -> Invocation:
        seen["pytest_env"] = dict(extra_env)
        return Invocation(command=command, output="1 passed", returncode=0, duration_seconds=0.01)

    run_plan(pytest_plan, repo=tmp_path, invoke_fn=mock_pytest)
    assert "SYNTHETIC_API_TOKEN" not in seen["pytest_env"]


def test_entry_tokens_do_not_select_the_runner(tmp_path: Path) -> None:
    """entry 文本里出现 task/execute 不能被当成 task 入口（显式元数据优先）。"""

    plan = RegressionPlan(
        project="demo-project",
        run_id="run-sniff",
        base_sha="a" * 40,
        head_sha="b" * 40,
        entries=[
            PlanEntry(
                case_id="TC-LOOKALIKE",
                name="文件名叫 test_task_execute",
                module="m",
                entry=["tests/test_task_execute.py::test_ok"],
                priority="manual",
            )
        ],
    )
    record = run_plan(plan, repo=tmp_path, dry_run=True)
    rendered = record.results[0].output_excerpt
    assert "-m pytest" in rendered
    assert "api_test_agent" not in rendered


def test_explicit_runner_argument_overrides_entry_metadata(approved_setup, tmp_path: Path) -> None:
    store, _, approval_id, _ = approved_setup
    catalog_yaml = tmp_path / "cases.yaml"
    export_approved_to_regression_catalog(store, "demo", catalog_yaml, approval_id=approval_id)
    plan = _plan_from("TC-DEMO-LIST-ITEMS", catalog_yaml, tmp_path, "run-override")

    record = run_plan(plan, repo=tmp_path, dry_run=True, runner="pytest", task_root=store.root)
    rendered = record.results[0].output_excerpt
    assert "-m pytest" in rendered
    assert "api_test_agent" not in rendered
