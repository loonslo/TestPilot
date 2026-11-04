"""Tests for F8 bridge: exporting approved task cases into regression catalog.

Also covers runner execution via task_binding.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from test_task_design import prepared as prepared
from test_task_execution import approved_setup as approved_setup

from api_test_agent.regression.models import PlanEntry, RegressionPlan
from api_test_agent.regression.project import load_catalog
from api_test_agent.regression.runner import Invocation, run_plan
from api_test_agent.tasks.bridge import export_approved_to_regression_catalog


def test_export_approved_to_regression_catalog(approved_setup, tmp_path: Path) -> None:
    store, config, approval_id, _ = approved_setup

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


def test_regression_runner_executes_bridged_task(approved_setup, tmp_path: Path) -> None:
    store, _, approval_id, _ = approved_setup
    catalog_yaml = tmp_path / "cases.yaml"

    export_approved_to_regression_catalog(
        store,
        "demo",
        catalog_yaml,
        approval_id=approval_id,
    )

    catalog = load_catalog(catalog_yaml, tmp_path / "smoke.yaml")
    case = catalog.get("TC-DEMO-LIST-ITEMS")
    assert case is not None

    plan_entry = PlanEntry.from_case(case, priority="manual")
    plan = RegressionPlan(
        project="demo-project",
        run_id="run-bridge-01",
        base_sha="a" * 40,
        head_sha="b" * 40,
        entries=[plan_entry],
    )

    # 1. Dry run verification: verifies task_command builder was invoked
    dry_record = run_plan(plan, repo=tmp_path, dry_run=True)
    assert len(dry_record.results) == 1
    output = dry_record.results[0].output_excerpt
    assert "api_test_agent.cli" in output
    assert "task" in output
    assert "execute" in output
    assert "--case-id" in output
    assert "list-items" in output

    # 2. Live execution via custom invoke_fn simulating successful task execution output
    captured_commands = []

    def mock_invoke(
        command: list[str], cwd: Path, extra_env: dict[str, str], timeout: float
    ) -> Invocation:
        captured_commands.append(command)
        simulated_output = (
            '{\n  "planned": 1,\n  "passed": 1,\n  "failed": 0\n}\n1 passed, 0 failed'
        )
        return Invocation(
            command=command,
            output=simulated_output,
            returncode=0,
            duration_seconds=0.05,
            timed_out=False,
            error="",
        )

    exec_record = run_plan(plan, repo=tmp_path, invoke_fn=mock_invoke)
    assert len(captured_commands) == 1
    assert "api_test_agent.cli" in captured_commands[0]
    assert "--case-id" in captured_commands[0]
    assert exec_record.results[0].result == "passed"
    assert exec_record.totals["passed"] == 1
