"""F6: Report generation and coverage accounting for TestPilot tasks.

Boundaries:
- Deterministic calculation: no hallucinated pass counts without evidence.
- Verified conservation law: planned == passed + failed + error + blocked + skipped + not_run.
- Separate accounting for discovered, confirmed, called operations.
- Separate accounting for 7 scenario categories (normal, validation, boundary, etc.).
- Explicit tables for unapproved/rejected/deferred cases and unresolved operations.
- Generates both Markdown (report.md) and machine-readable JSON (report.json).
- Supports tasks in any state (completed, incomplete, running, awaiting_review, etc.).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from api_test_agent.tasks.approval import read_approval
from api_test_agent.tasks.design import SCENARIOS, load_inventory
from api_test_agent.tasks.discovery import Inventory
from api_test_agent.tasks.models import (
    ExecutionAccounting,
    OperationCoverage,
    Report,
    ScenarioCoverage,
)
from api_test_agent.tasks.store import TaskStore, _write_new


def generate_report(
    store: TaskStore,
    task_id: str,
    *,
    execution_id: str | None = None,
    output_dir: Path | None = None,
) -> tuple[Report, Path, Path]:
    task = store.load(task_id)

    # 1. Load inventory if available
    inventory: Inventory | None = None
    try:
        inventory = load_inventory(store, task_id)
    except Exception:
        inventory = None

    # 2. Load active approval if exists
    active_approval_id = None
    pointer_approval = store._folder(task_id) / "active-approval.json"
    if pointer_approval.is_file():
        try:
            active_approval_id = json.loads(pointer_approval.read_text("utf-8")).get("approval_id")
        except Exception:
            pass

    approval_snapshot = None
    if active_approval_id:
        try:
            approval_snapshot = read_approval(store, task_id, active_approval_id)
        except Exception:
            pass

    # 3. Locate execution
    if execution_id is None:
        pointer_exec = store._folder(task_id) / "active-execution.json"
        if pointer_exec.is_file():
            try:
                execution_id = json.loads(pointer_exec.read_text("utf-8")).get("execution_id")
            except Exception:
                pass

    records = store.execution_records(task_id, execution_id) if execution_id else []
    issues = store.issues(task_id, execution_id) if execution_id else []

    # 4. Compute accounting
    planned = len(approval_snapshot.cases) if approval_snapshot else 0
    passed = 0
    failed = 0
    error = 0
    blocked = 0
    skipped = 0
    not_run = 0
    evidence_backed_count = 0
    missing_evidence_count = 0
    missing_evidence_ids: list[str] = []
    record_evidence_status: dict[str, bool] = {}

    approved_cases = (
        {case.fingerprint(): case for case in approval_snapshot.cases} if approval_snapshot else {}
    )
    for r in records:
        if r.outcome in {"passed", "failed", "error"}:
            r_missing_ids: list[str] = []
            if not r.evidence_ids:
                r_missing_ids.append(f"missing_evidence_for_{r.revision_hash[:16]}")
            else:
                for ev_id in r.evidence_ids:
                    try:
                        ev = store.load_evidence(task_id, ev_id, execution_id)
                    except ValueError, OSError:
                        ev = None
                    case = approved_cases.get(r.revision_hash)
                    step = (
                        next((s for s in case.steps if ev and s.step_id == ev.step_id), None)
                        if case
                        else None
                    )
                    if (
                        ev is None
                        or ev.task_id != task_id
                        or ev.evidence_id != ev_id
                        or case is None
                        or ev.case_id != case.case_id
                        or step is None
                        or ev.operation_id != step.operation_id
                    ):
                        r_missing_ids.append(ev_id)
            if r_missing_ids:
                missing_evidence_count += 1
                missing_evidence_ids.extend(r_missing_ids)
                record_evidence_status[r.revision_hash] = False
            else:
                evidence_backed_count += 1
                record_evidence_status[r.revision_hash] = True
                if r.outcome == "passed":
                    passed += 1
                elif r.outcome == "failed":
                    failed += 1
                elif r.outcome == "error":
                    error += 1
        elif r.outcome == "blocked":
            blocked += 1
            record_evidence_status[r.revision_hash] = True
        elif r.outcome == "skipped":
            skipped += 1
            record_evidence_status[r.revision_hash] = True
        elif r.outcome == "not_run":
            not_run += 1
            record_evidence_status[r.revision_hash] = True

    # If execution hasn't run yet, but approval exists:
    if not records and planned > 0:
        not_run = planned

    conserved = (
        planned == (passed + failed + error + blocked + skipped + not_run)
        and missing_evidence_count == 0
    )

    accounting = ExecutionAccounting(
        planned=planned,
        passed=passed,
        failed=failed,
        error=error,
        blocked=blocked,
        skipped=skipped,
        not_run=not_run,
        conserved=conserved,
        evidence_backed_count=evidence_backed_count,
        missing_evidence_count=missing_evidence_count,
        missing_evidence_ids=tuple(missing_evidence_ids),
    )

    # 5. Operation Coverage: calculated strictly from step evidence
    called_operations: set[str] = set()
    called_by_revision: dict[str, set[str]] = {}
    executed_case_hashes = {
        r.revision_hash
        for r in records
        if r.outcome in {"passed", "failed", "error"}
        and record_evidence_status.get(r.revision_hash, False)
    }

    step_to_op: dict[tuple[str, str], str] = {}
    if approval_snapshot:
        for c in approval_snapshot.cases:
            for s in c.steps:
                step_to_op[(c.case_id, s.step_id)] = s.operation_id

    for r in records:
        if not record_evidence_status.get(r.revision_hash, False):
            continue
        for ev_id in r.evidence_ids:
            ev = store.load_evidence(task_id, ev_id, execution_id)
            if ev is not None:
                if not ev.response_received:
                    continue
                op_id = getattr(ev, "operation_id", None) or step_to_op.get(
                    (ev.case_id, ev.step_id)
                )
                if op_id:
                    called_operations.add(op_id)
                    called_by_revision.setdefault(r.revision_hash, set()).add(op_id)

    total_discovered = len(inventory.operations) if inventory else 0
    confirmed_in_scope = (
        sum(1 for op in inventory.operations if op.status == "confirmed") if inventory else 0
    )
    supported = (
        sum(1 for op in inventory.operations if op.status == "confirmed") if inventory else 0
    )
    if inventory:
        called_operations.intersection_update(
            op.operation_id for op in inventory.operations if op.status == "confirmed"
        )
    called_count = len(called_operations)
    op_coverage_rate = (
        round(called_count / confirmed_in_scope, 4) if confirmed_in_scope > 0 else 0.0
    )

    op_coverage = OperationCoverage(
        total_discovered=total_discovered,
        confirmed_in_scope=confirmed_in_scope,
        supported=supported,
        called=called_count,
        coverage_rate=op_coverage_rate,
    )

    # 6. Scenario Coverage
    all_cases = store.cases(task_id)
    designed_scenarios: set[tuple[str, str]] = set()
    executed_scenarios: set[tuple[str, str]] = set()
    for c in all_cases:
        for op_id in c.operation_ids:
            designed_scenarios.add((op_id, c.scenario))
            if (
                op_id in called_operations
                and op_id in called_by_revision.get(c.fingerprint(), set())
                and c.fingerprint() in executed_case_hashes
            ):
                executed_scenarios.add((op_id, c.scenario))

    total_slots = total_discovered * len(SCENARIOS)
    sc_coverage_rate = round(len(executed_scenarios) / total_slots, 4) if total_slots > 0 else 0.0

    sc_coverage = ScenarioCoverage(
        total_slots=total_slots,
        designed=len(designed_scenarios),
        executed=len(executed_scenarios),
        coverage_rate=sc_coverage_rate,
    )

    # Uncovered and out-of-scope operations
    uncovered_ops = []
    limitations = []
    if inventory:
        for op in inventory.operations:
            if op.operation_id not in called_operations:
                uncovered_ops.append(f"{op.method} {op.path} ({op.status}: {op.reason})")
            if op.status in {"unsupported", "out_of_scope", "conflict", "unresolved"}:
                limitations.append(f"{op.method} {op.path}: {op.status} - {op.reason}")

    report_id = uuid4().hex
    report_model = Report(
        report_id=report_id,
        task_id=task_id,
        generated_at=datetime.now(UTC),
        task_status=task.status,
        target_project=task.target_project,
        target_commit=task.target_commit,
        repository=task.repository,
        environment=task.environment,
        version_verified=False,
        approval_id=active_approval_id,
        execution_id=execution_id,
        accounting=accounting,
        operation_coverage=op_coverage,
        scenario_coverage=sc_coverage,
        records=tuple(records),
        issues=tuple(issues),
        uncovered_operations=tuple(uncovered_ops),
        limitations=tuple(limitations),
        missing_evidence_ids=tuple(missing_evidence_ids),
    )

    # 7. Render Markdown
    md_lines: list[str] = [
        f"# TestPilot 任务报告：{task_id}",
        "",
        f"- **生成时间**：{report_model.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- **任务状态**：`{task.status}`",
        f"- **目标项目**：`{task.target_project or '未指定'}`",
        f"- **目标提交**：`{task.target_commit or '未指定'}`",
        f"- **测试环境**：`{task.environment or '未指定'}`（version_verified: false）",
        f"- **批准编号**：`{active_approval_id or '无'}`",
        f"- **执行编号**：`{execution_id or '无'}`",
        "",
        "## 1. 执行与守恒对账（Conservation Accounting）",
        "",
        "> [!IMPORTANT]",
        f"> **计划执行守恒公式**：`计划执行数 ({accounting.planned}) = "
        f"passed ({accounting.passed}) + failed ({accounting.failed}) + "
        f"error ({accounting.error}) + blocked ({accounting.blocked}) + "
        f"skipped ({accounting.skipped}) + not_run ({accounting.not_run})`",
        (
            "> **守恒结果**：`"
            f"{'PASS (守恒公式成立)' if accounting.conserved else 'FAIL (不平衡/证据缺失)'}`"
        ),
        "",
        "| 指标 | 数量 | 说明 |",
        "| --- | --- | --- |",
        f"| 计划执行数 (Planned) | {accounting.planned} | 冻结批准清单中的用例总数 |",
        f"| 通过 (Passed) | {accounting.passed} | 主体步骤与断言全部通过且证据已落盘 |",
        f"| 失败 (Failed) | {accounting.failed} | 断言不符预期，证据已落盘 |",
        f"| 异常 (Error) | {accounting.error} | 网络连接、解析或执行异常 |",
        f"| 阻塞 (Blocked) | {accounting.blocked} | 前置条件缺失导致未进入测试 |",
        f"| 跳过 (Skipped) | {accounting.skipped} | 显式规则条件跳过 |",
        f"| 未执行 (Not Run) | {accounting.not_run} | 中断、限额或未执行到的剩余项 |",
        f"| 有凭据执行数 | {accounting.evidence_backed_count} | 严格验证落盘证据文件的执行数 |",
        f"| 缺失凭据执行数 | {accounting.missing_evidence_count} | 声明完成但落盘证据文件缺失 |",
        "",
    ]

    if accounting.missing_evidence_count > 0:
        md_lines.extend(
            [
                "> [!CAUTION]",
                (
                    f"> **证据缺失警告**：检测到 {accounting.missing_evidence_count} "
                    "条执行记录缺失有效落盘证据文件！"
                ),
                "> 缺失证据的用例不计入通过/失败统计，执行对账标记为不守恒 (FAIL)。",
                f"> **缺失凭据 ID**：`{', '.join(accounting.missing_evidence_ids)}`",
                "",
            ]
        )

    # Operation Coverage Section
    md_lines.extend(
        [
            "## 2. 接口覆盖统计",
            "",
            "| 接口统计项 | 数值 | 覆盖率说明 |",
            "| --- | --- | --- |",
            f"| 发现接口总数 | {op_coverage.total_discovered} | 全部发现的接口 |",
            f"| 范围内已确认接口 | {op_coverage.confirmed_in_scope} | scope 内且 confirmed |",
            f"| 支持自动执行接口 | {op_coverage.supported} | 当前支持的 HTTP 接口 |",
            f"| 实际调用接口数 | {op_coverage.called} | 有真实 HTTP 调用的接口 |",
            f"| 接口调用覆盖率 | {op_coverage.coverage_rate * 100:.1f}% | called/confirmed |",
            "",
        ]
    )

    # Scenario Coverage Section
    md_lines.extend(
        [
            "## 3. 场景矩阵覆盖",
            "",
            f"- **场景矩阵总槽位**：{sc_coverage.total_slots}（{total_discovered} 接口 × 7）",
            f"- **已设计场景数**：{sc_coverage.designed}",
            f"- **已执行场景数**：{sc_coverage.executed}",
            f"- **场景执行覆盖率**：{sc_coverage.coverage_rate * 100:.1f}%",
            "",
        ]
    )

    # Case Execution Details Section
    md_lines.extend(
        [
            "## 4. 用例执行结果明细",
            "",
            "| 用例编号 | 结果 | 尝试 | 凭据数量 | 原因与说明 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for r in records:
        h16 = r.revision_hash[:16]
        is_backed = record_evidence_status.get(r.revision_hash, False)
        outcome_disp = r.outcome if is_backed else f"{r.outcome} (证据缺失)"
        md_lines.append(
            f"| `{h16}` | `{outcome_disp}` | {r.attempt} | {len(r.evidence_ids)} | {r.reason} |"
        )
    if not records:
        md_lines.append("| (无执行记录) | - | - | - | 尚未执行任何用例 |")
    md_lines.append("")

    # Issues Section
    md_lines.extend(
        [
            "## 5. 问题与缺陷清单（Issues）",
            "",
            "| 问题编号 | 类别 | 标题 | 状态 | 关联用例 | 详细信息 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for issue in issues:
        cid = issue.case_id or "-"
        md_lines.append(
            f"| `{issue.issue_id}` | `{issue.category}` | {issue.title} | `{issue.status}` | "
            f"`{cid}` | {issue.detail} |"
        )
    if not issues:
        md_lines.append("| (无发现问题) | - | 未发现缺陷或异常 | - | - | - |")
    md_lines.append("")

    # Unapproved and Deferred Cases Section
    review_states = store.review_states(task_id)
    non_approved = {cid: state for cid, state in review_states.items() if state != "approved"}
    md_lines.extend(
        [
            "## 6. 未批准、暂缓与拒绝用例（单独统计）",
            "",
            "| 用例编号 | 当前审核状态 | 说明 |",
            "| --- | --- | --- |",
        ]
    )
    for cid, state in non_approved.items():
        md_lines.append(f"| `{cid}` | `{state}` | 未进入本次冻结执行清单 |")
    if not non_approved:
        md_lines.append("| (无) | - | 全部用例均已进入批准决定 |")
    md_lines.append("")

    # Limitations & Uncovered Operations
    md_lines.extend(
        [
            "## 7. 运行限制与未覆盖说明",
            "",
        ]
    )
    if limitations:
        for lim in limitations:
            md_lines.append(f"- **限制**：{lim}")
    else:
        md_lines.append("- 无特殊运行限制记录。")
    md_lines.append("")

    if uncovered_ops:
        for operation in uncovered_ops:
            md_lines.append(f"- **未覆盖接口**：{operation}")
    else:
        md_lines.append("- 当前清单中无未覆盖接口。")
    md_lines.append("")

    md_content = "\n".join(md_lines) + "\n"

    # Save destinations
    dest_dir = output_dir or (store._folder(task_id) / "reports" / report_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    json_path = dest_dir / "report.json"
    md_path = dest_dir / "report.md"

    _write_new(json_path, report_model)
    md_path.write_text(md_content, encoding="utf-8")

    # Link/update active-report.json in task folder
    (store._folder(task_id) / "active-report.json").write_text(
        json.dumps({"report_id": report_id, "report_path": str(json_path)}),
        encoding="utf-8",
    )
    # Also write top-level report.md in task folder for easy human reading
    (store._folder(task_id) / "report.md").write_text(md_content, encoding="utf-8")

    return report_model, md_path, json_path
