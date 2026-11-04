import json

import httpx
import pytest
from test_task_design import prepared as prepared
from test_task_execution import approved_setup as approved_setup

from api_test_agent.cli import main
from api_test_agent.tasks.approval import ExecutionConfig, ReviewSubmission, approve, fingerprint
from api_test_agent.tasks.design import export_review, load_inventory
from api_test_agent.tasks.discovery import discover
from api_test_agent.tasks.execution import execute_task
from api_test_agent.tasks.models import Assertion, CaseRevision, Rule, Step, Task
from api_test_agent.tasks.report import generate_report
from api_test_agent.tasks.store import TaskStore


def test_generate_report_completed_task(approved_setup):
    store, config, approval_id, _ = approved_setup

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": 1}]})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    execute_task(store, "demo", approval_id=approval_id, config=config, http_client=client)

    rep, md_path, json_path = generate_report(store, "demo")

    assert md_path.is_file()
    assert json_path.is_file()
    assert rep.task_id == "demo"
    assert rep.task_status == "completed"
    assert rep.accounting.planned == 2
    assert rep.accounting.passed == 2
    assert rep.accounting.not_run == 0
    assert rep.accounting.conserved is True
    assert rep.accounting.evidence_backed_count == 2
    assert rep.operation_coverage.called >= 1
    assert rep.operation_coverage.coverage_rate > 0.0

    md_text = md_path.read_text("utf-8")
    assert "计划执行守恒公式" in md_text
    assert "PASS (守恒公式成立)" in md_text
    assert "接口覆盖统计" in md_text
    assert "场景矩阵覆盖" in md_text
    assert "用例执行结果明细" in md_text


def test_generate_report_interrupted_task(approved_setup):
    store, config, approval_id, _ = approved_setup

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    execute_task(
        store, "demo", approval_id=approval_id, config=config, max_cases=1, http_client=client
    )

    rep, md_path, json_path = generate_report(store, "demo")

    assert rep.task_status == "incomplete"
    assert rep.accounting.planned == 2
    assert rep.accounting.passed == 1
    assert rep.accounting.not_run == 1
    assert rep.accounting.conserved is True

    md_text = md_path.read_text("utf-8")
    assert "未执行 (Not Run)" in md_text
    assert "| 1 |" in md_text


def test_generate_report_before_execution(approved_setup):
    store, _, approval_id, _ = approved_setup

    rep, md_path, json_path = generate_report(store, "demo")

    assert rep.task_status == "ready"
    assert rep.accounting.planned == 2
    assert rep.accounting.passed == 0
    assert rep.accounting.not_run == 2
    assert rep.accounting.conserved is True
    assert rep.accounting.evidence_backed_count == 0

    md_text = md_path.read_text("utf-8")
    assert "尚未执行任何用例" in md_text


def test_cli_execute_and_report_commands(approved_setup, monkeypatch, capsys):
    store, config, approval_id, _ = approved_setup

    # Mock execute_task in CLI to avoid real socket calls
    from api_test_agent.tasks import execution

    def fake_execute(store, task_id, **kwargs):
        # Return an accounting directly
        from api_test_agent.tasks.models import ExecutionAccounting

        return ExecutionAccounting(
            planned=2,
            passed=2,
            failed=0,
            error=0,
            blocked=0,
            skipped=0,
            not_run=0,
            conserved=True,
            evidence_backed_count=2,
        )

    monkeypatch.setattr(execution, "execute_task", fake_execute)

    root_args = ["task", "--root", str(store.root)]

    # Run execute
    rc = main([*root_args, "execute", "demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"passed": 2' in out
    assert '"conserved": true' in out

    # Run report
    rc = main([*root_args, "report", "demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "report written:" in out


def test_generate_report_missing_evidence_detected_and_fails_conservation(approved_setup):
    store, config, approval_id, _ = approved_setup

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": 1}]})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    execute_task(store, "demo", approval_id=approval_id, config=config, http_client=client)

    # Delete all evidence files on disk
    active_exec = store.folder("demo") / "active-execution.json"
    import json

    exec_id = json.loads(active_exec.read_text("utf-8"))["execution_id"]
    evidence_dir = store.folder("demo") / "executions" / exec_id / "evidence"
    evidence_files = list(evidence_dir.glob("*.json"))
    assert len(evidence_files) == 2
    for ef in evidence_files:
        ef.unlink()

    rep, md_path, json_path = generate_report(store, "demo")

    assert rep.accounting.planned == 2
    assert rep.accounting.passed == 0
    assert rep.accounting.evidence_backed_count == 0
    assert rep.accounting.missing_evidence_count == 2
    assert rep.accounting.conserved is False
    assert len(rep.accounting.missing_evidence_ids) == 2

    md_text = md_path.read_text("utf-8")
    assert "FAIL (不平衡/证据缺失)" in md_text
    assert "证据缺失警告" in md_text
    assert "缺失凭据执行数 | 2" in md_text
    assert "缺失凭据" in md_text


def test_report_operation_coverage_strict_from_step_evidence(tmp_path):
    (tmp_path / "api.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {
                    "/setup": {
                        "post": {
                            "operationId": "setup_op",
                            "responses": {"200": {"description": "Setup"}},
                        }
                    },
                    "/items": {
                        "get": {
                            "operationId": "items_op",
                            "responses": {"200": {"description": "Items"}},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="cov_demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json"],
        )
    )
    discover(store, "cov_demo", tmp_path)
    inv = load_inventory(store, "cov_demo")
    assert len(inv.operations) == 2
    sources = inv.materials
    op_setup = next(o for o in inv.operations if o.path == "/setup").operation_id
    op_items = next(o for o in inv.operations if o.path == "/items").operation_id

    case = CaseRevision(
        case_id="flow-case",
        revision=1,
        title="Flow test with prepare and test",
        origin="human",
        target_commit="a" * 40,
        operation_ids=(op_setup, op_items),
        purpose="Test multi-step operation coverage",
        scenario="normal",
        rules=(
            Rule(
                rule_id="r1",
                statement="Setup rule",
                basis="requirement",
                sources=(sources[0],),
            ),
        ),
        steps=(
            Step(
                step_id="step1",
                phase="prepare",
                operation_id=op_setup,
                account="anonymous",
                side_effect=True,
            ),
            Step(
                step_id="step2",
                phase="test",
                operation_id=op_items,
                account="anonymous",
                depends_on=("step1",),
                side_effect=False,
            ),
        ),
        assertions=(
            Assertion(
                expression="{{step2.status}} == 200",
                expected=True,
                rule_ids=("r1",),
            ),
        ),
        change_summary="Initial multi-step",
    )
    store.add_case("cov_demo", case)
    config = ExecutionConfig(
        environment="synthetic",
        target_commit="a" * 40,
        base_url="http://127.0.0.1:18081",
        allowed_operations=(op_setup, op_items),
    )
    review = export_review(store, "cov_demo", config)
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))

    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human-reviewer",
        decisions=[
            dict(
                case_id=case.case_id,
                revision_hash=case.fingerprint(),
                decision="approved",
                reason="Approved flow",
            )
        ],
    )
    folder = approve(store, "cov_demo", submission, config)

    # Prepare step returns 500 error, so test step2 is NEVER executed
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "prepare failed"})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    acc = execute_task(
        store, "cov_demo", approval_id=folder.name, config=config, http_client=client
    )

    assert acc.error == 1
    assert acc.passed == 0

    rep, md_path, _ = generate_report(store, "cov_demo")

    # Only setup_op was actually called (with evidence). items_op was NOT called!
    assert rep.operation_coverage.confirmed_in_scope == 2
    assert rep.operation_coverage.called == 1
    assert any("items_op" in op or "/items" in op for op in rep.uncovered_operations)
    assert not any("setup_op" in op for op in rep.uncovered_operations)
    md_text = md_path.read_text("utf-8")
    assert all(f"- **未覆盖接口**：{op}" in md_text for op in rep.uncovered_operations)
    assert "- **未覆盖接口**：POST /setup" not in md_text


def test_transport_failure_does_not_count_as_operation_coverage(approved_setup):
    store, _, _, _ = approved_setup

    def handler(request):
        raise httpx.ConnectError("synthetic connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execute_task(store, "demo", http_client=client)
    report, _, _ = generate_report(store, "demo")
    assert report.accounting.error == 2
    assert report.operation_coverage.called == 0
    assert report.scenario_coverage.executed == 0


@pytest.mark.parametrize("corruption", ["invalid_json", "wrong_case", "wrong_operation"])
def test_corrupt_evidence_is_reported_as_missing(approved_setup, corruption):
    store, _, _, _ = approved_setup
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"items": []}))
    ) as client:
        execute_task(store, "demo", http_client=client)
    pointer = json.loads((store.folder("demo") / "active-execution.json").read_text("utf-8"))
    folder = store.folder("demo") / "executions" / pointer["execution_id"] / "evidence"
    for path in folder.glob("*.json"):
        if corruption == "invalid_json":
            path.write_text("{invalid json", encoding="utf-8")
        else:
            data = json.loads(path.read_text("utf-8"))
            data["case_id" if corruption == "wrong_case" else "operation_id"] = "unrelated"
            path.write_text(json.dumps(data), encoding="utf-8")
    report, _, _ = generate_report(store, "demo")
    assert report.accounting.passed == 0
    assert report.accounting.missing_evidence_count == 2
    assert not report.accounting.conserved


def test_scenarios_do_not_borrow_other_cases_operation_evidence(approved_setup):
    store, _, _, _ = approved_setup
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("synthetic refused", request=request)
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execute_task(store, "demo", http_client=client)
    report, _, _ = generate_report(store, "demo")
    assert report.operation_coverage.called == 1
    assert report.scenario_coverage.executed == 1


@pytest.mark.parametrize("execute", [False, True])
def test_markdown_lists_the_same_uncovered_operations_as_json(approved_setup, execute):
    store, _, _, _ = approved_setup
    if execute:
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"items": []}))
        ) as client:
            execute_task(store, "demo", http_client=client)
    report, md_path, json_path = generate_report(store, "demo")
    markdown = md_path.read_text("utf-8")
    saved_json = json.loads(json_path.read_text("utf-8"))
    assert saved_json["uncovered_operations"] == list(report.uncovered_operations)
    if execute:
        assert not report.uncovered_operations
        assert "- 当前清单中无未覆盖接口。" in markdown
        assert "- **未覆盖接口**：" not in markdown
    else:
        assert report.uncovered_operations
        for operation in report.uncovered_operations:
            assert f"- **未覆盖接口**：{operation}" in markdown
