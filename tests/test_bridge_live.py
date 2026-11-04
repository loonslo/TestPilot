"""真实桥接验证：真实子进程 + 真实本地 HTTP 服务，覆盖两条用例连续执行。

这些测试不使用模拟子进程输出：每条用例都由独立的 `python -m api_test_agent.cli task
execute` 子进程执行，并对一个只绑定在 127.0.0.1 的合成 HTTP 服务发请求。合成凭据由父进程
环境提供，用来证明凭据按白名单转发到 task 子进程。
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from test_task_design import prepared as prepared

from api_test_agent.regression.models import ROOT_PLACEHOLDER, PlanEntry, RegressionPlan
from api_test_agent.regression.project import load_catalog
from api_test_agent.regression.runner import run_plan, task_command
from api_test_agent.tasks.approval import (
    ExecutionConfig,
    ReviewSubmission,
    approve,
    fingerprint,
)
from api_test_agent.tasks.bridge import export_approved_to_regression_catalog
from api_test_agent.tasks.design import design, export_review

TOKEN_ENV = "SYNTHETIC_API_TOKEN"
TOKEN_VALUE = "live-bridge-secret-token"


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        payload = b'{"items": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # 保持测试输出干净
        return


@pytest.fixture
def live_site():
    _Recorder.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), _Recorder.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def live_setup(prepared, live_site, monkeypatch):
    port, requests = live_site
    store, inventory, output = prepared
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)

    case = deepcopy(output["cases"][0])
    case["steps"][0]["account"] = "synthetic"
    second = deepcopy(case)
    second["case_id"] = "list-items-2"
    second["scenario"] = "boundary"
    output["cases"] = [case, second]
    for assessment in output["assessments"]:
        if assessment["scenario"] in {"normal", "boundary"}:
            assessment["status"] = "designed"
            assessment["reason"] = "Synthetic baseline for the live bridge"

    design(store, "demo", supplied_response=json.dumps(output))
    config = ExecutionConfig(
        environment="synthetic",
        target_commit="a" * 40,
        base_url=f"http://127.0.0.1:{port}",
        allowed_operations=[inventory.operations[0].operation_id],
        accounts={"synthetic": TOKEN_ENV},
    )
    review = export_review(store, "demo", config)
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human-reviewer",
        decisions=[
            dict(
                case_id=c.case_id,
                revision_hash=c.fingerprint(),
                decision="approved",
                reason="Approved synthetic live bridge baseline",
            )
            for c in store.cases("demo")
        ],
    )
    folder = approve(store, "demo", submission, config)
    catalog_path = Path(store.root) / "cases.yaml"
    export_approved_to_regression_catalog(store, "demo", catalog_path, approval_id=folder.name)
    return store, catalog_path, requests


def _plan(catalog_path: Path, base: Path, run_id: str) -> RegressionPlan:
    catalog = load_catalog(catalog_path, base / "smoke.yaml")
    entries = [
        PlanEntry.from_case(case, priority="manual")
        for case in catalog.cases
        if case.id.startswith("TC-DEMO-LIST-ITEMS")
    ]
    assert len(entries) == 2
    return RegressionPlan(
        project="demo-project",
        run_id=run_id,
        base_sha="a" * 40,
        head_sha="b" * 40,
        entries=entries,
    )


def test_two_bridged_cases_run_through_real_subprocesses(live_setup, tmp_path: Path) -> None:
    store, catalog_path, requests = live_setup
    plan = _plan(catalog_path, tmp_path, "live-run-01")

    record = run_plan(
        plan,
        repo=tmp_path,
        python=sys.executable,
        per_case_timeout=180,
        task_root=store.root,
    )

    assert [r.result for r in record.results] == ["passed", "passed"], record.errors
    assert record.totals["passed"] == 2
    assert record.state == "completed"
    assert len(requests) == 2
    # 两条用例各自一个执行编号，且任务没有被第一条用例锁死
    assert store.load("demo").status == "ready"
    executions = list((store.folder("demo") / "executions").iterdir())
    assert len(executions) == 2

    # 凭据按白名单转到子进程：服务端确实收到 Bearer 令牌
    assert [auth for _, auth in requests] == [f"Bearer {TOKEN_VALUE}"] * 2
    # 凭据值不落盘
    assert TOKEN_VALUE not in catalog_path.read_text("utf-8")
    run_dump = record.model_dump_json()
    assert TOKEN_VALUE not in run_dump


def test_replaying_the_same_plan_keeps_both_cases_executable(live_setup, tmp_path: Path) -> None:
    """一轮跑完（含任务已完全执行）后，同一批准集合仍能作为下一轮回归重放。"""

    store, catalog_path, requests = live_setup
    plan = _plan(catalog_path, tmp_path, "live-run-02")

    first = run_plan(plan, repo=tmp_path, python=sys.executable, task_root=store.root)
    assert [r.result for r in first.results] == ["passed", "passed"]
    assert store.load("demo").status == "ready"

    second = run_plan(plan, repo=tmp_path, python=sys.executable, task_root=store.root)
    assert [r.result for r in second.results] == ["passed", "passed"], second.errors
    assert len(requests) == 4


def test_zero_case_budget_is_not_reported_as_passed(live_setup, tmp_path: Path) -> None:
    store, catalog_path, requests = live_setup
    plan = _plan(catalog_path, tmp_path, "live-run-03")
    entry = plan.entries[0]
    entry.entry = [*entry.entry, "--max-cases", "0"]
    plan.entries = [entry]

    record = run_plan(plan, repo=tmp_path, python=sys.executable, task_root=store.root)

    result = record.results[0]
    assert result.result != "passed", result.output_excerpt
    assert result.accounting is not None
    assert result.accounting["not_run"] == 1
    assert record.totals["passed"] == 0
    assert record.state == "incomplete"
    assert requests == []  # 一条请求都没有发出


def test_zero_case_budget_exit_code_is_not_success(live_setup, tmp_path: Path) -> None:
    """CLI 自己也不能把零执行报告成成功：退出码必须是 2。"""

    store, catalog_path, _ = live_setup
    plan = _plan(catalog_path, tmp_path, "live-run-04")
    entry = plan.entries[0]
    tokens = [str(store.root) if token == ROOT_PLACEHOLDER else token for token in entry.entry] + [
        "--max-cases",
        "0",
    ]
    command = task_command(sys.executable, tokens, tmp_path, None)

    proc = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "task outcome: incomplete" in proc.stdout
    assert "not_run=1" in proc.stdout
