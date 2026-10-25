from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_suite

from api_test_agent.regression.collect import collect
from api_test_agent.regression.models import ImpactAnalysis, ImpactItem
from api_test_agent.regression.plan import plan_from_analysis
from api_test_agent.regression.runner import RunError, classify, pytest_command, run_plan


def _plan(project, run_id="r1"):
    context = collect(project, "HEAD~1", "HEAD")
    analysis = ImpactAnalysis(
        summary="x",
        impacts=[
            ImpactItem(
                id="IMP-01",
                change="库存校验",
                module="订单",
                evidence="direct",
                existing_cases=["TC-A", "TC-B"],
            )
        ],
        recommended_cases=["TC-A", "TC-B"],
    )
    return plan_from_analysis(
        run_id=run_id,
        context=context,
        analysis=analysis,
        catalog=project.catalog,
        module_map=project.module_map,
    )


def test_classify_maps_exit_codes():
    assert classify(0, "", False) == "passed"
    assert classify(1, "", False) == "failed"
    assert classify(5, "", False) == "error"  # 没收集到用例不是通过
    assert classify(None, "", True) == "error"
    assert classify(None, "", False) == "error"


def test_runner_executes_real_pytest_and_reports_true_results(worlds, project):
    repo = worlds[1]
    make_suite(repo)
    plan = _plan(project)
    record = run_plan(plan, repo=repo, per_case_timeout=120)
    results = {r.case_id: r.result for r in record.results}
    assert results["TC-A"] == "passed"
    assert results["TC-B"] == "failed"  # 断言失败必须如实记录
    assert record.totals["passed"] == 1
    assert record.totals["failed"] == 1
    assert record.totals["executed"] == 2
    assert record.state == "completed"
    failed = next(r for r in record.results if r.case_id == "TC-B")
    assert failed.assertions
    assert "assert" in failed.output_excerpt or "1 + 1 == 3" in failed.output_excerpt


def test_runner_marks_missing_entry_as_error_not_pass(worlds, project):
    plan = _plan(project)
    # 仓库里没有测试文件：pytest 退出码 4（用法错误）或 5（未收集到），都不能算通过
    record = run_plan(plan, repo=worlds[1], per_case_timeout=120)
    assert all(r.result == "error" for r in record.results)
    assert record.totals["passed"] == 0


def test_runner_timeout_is_error(worlds, project):
    plan = _plan(project)
    record = run_plan(plan, repo=worlds[1], per_case_timeout=0.001)
    assert all(r.result == "error" for r in record.results)
    assert record.errors


def test_runner_refuses_empty_plan(worlds, project):
    plan = _plan(project)
    plan.entries = []
    with pytest.raises(RunError):
        run_plan(plan, repo=worlds[1])


def test_dry_run_never_executes(worlds, project):
    repo = worlds[1]
    make_suite(repo)
    plan = _plan(project)
    record = run_plan(plan, repo=repo, dry_run=True)
    assert all(r.result == "skipped" for r in record.results)
    assert record.totals["executed"] == 0
    assert record.state == "planned"


def test_pytest_command_is_pinned_and_shell_free(tmp_path: Path):
    command = pytest_command("py -3", ["tests/x.py::t"], tmp_path, ["-q"])
    assert command[0] == "py"
    assert "-m" in command and "pytest" in command
    assert command[-1] == "tests/x.py::t"
    assert not any(ch in " ".join(command) for ch in ";|&")


def test_runner_ignores_credentials_in_env(worlds, project, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "secret")
    monkeypatch.setenv("SOME_TOKEN", "secret")
    from api_test_agent.regression.runner import _env

    env = _env({"EXTRA": "1"})
    assert "SOME_API_KEY" not in env
    assert "SOME_TOKEN" not in env
    assert env["EXTRA"] == "1"
