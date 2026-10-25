from __future__ import annotations

from datetime import UTC, datetime

from api_test_agent.regression.collect import collect
from api_test_agent.regression.models import (
    CaseRunResult,
    ImpactAnalysis,
    ImpactItem,
    NewCaseSuggestion,
    RunRecord,
)
from api_test_agent.regression.plan import plan_from_analysis
from api_test_agent.regression.report import render, summarize, write_report


def _fixtures(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    analysis = ImpactAnalysis(
        summary="下单接口增加库存校验",
        impacts=[
            ImpactItem(
                id="IMP-01",
                change="create_order 增加 qty>10 校验",
                module="订单",
                evidence="direct",
                source_files=["src/orders/api.py"],
                source_lines=["src/orders/api.py:2-3"],
                business_source=["docs/orders.md"],
                existing_cases=["TC-A"],
            )
        ],
        new_case_suggestions=[NewCaseSuggestion(title="库存不足返回 400", module="订单")],
        recommended_cases=["TC-A", "TC-GHOST"],
        uncertain=["阈值是否可配置"],
    )
    plan = plan_from_analysis(
        run_id="r1",
        context=context,
        analysis=analysis,
        catalog=project.catalog,
        module_map=project.module_map,
    )
    plan.environment_version = "stage-2026.10.22"
    plan.environment_confirmed = True
    return context, analysis, plan


def _record(plan, results):
    record = RunRecord(
        run_id=plan.run_id,
        project=plan.project,
        plan_sha256=plan.plan_sha256,
        environment_version=plan.environment_version,
        state="completed",
    )
    for entry, outcome in results:
        record.results.append(
            CaseRunResult(
                case_id=entry,
                name=entry,
                entry=["tests/x.py"],
                priority="smoke",
                result=outcome,
                duration_seconds=1.0,
                assertions=["E   assert 1 + 1 == 3"] if outcome == "failed" else [],
                executed_at=datetime.now(UTC),
            )
        )
    record.recount()
    return record


def test_report_never_counts_unexecuted_as_pass(worlds):
    context, analysis, plan = _fixtures(worlds)
    record = _record(plan, [("TC-A", "passed")])
    text = render(plan, record, analysis=analysis, context=context)
    stats = summarize(plan, record)
    assert stats["planned"] == len(plan.entries)
    assert stats["executed"] == 1
    assert stats["not_run"] == len(plan.entries) - 1
    assert "未执行" in text
    assert "不计入通过数" in text or "不计入通过" in text


def test_report_lists_failures_and_gaps_and_rejections(worlds):
    context, analysis, plan = _fixtures(worlds)
    record = _record(plan, [("TC-A", "failed")])
    text = render(plan, record, analysis=analysis, context=context)
    assert "TC-GHOST" in text  # 被剔除的引用要显示原因
    assert "缺口" in text
    assert "失败断言" in text
    assert "docs/orders.md" in text
    assert "stage-2026.10.22" in text
    assert "direct" in text or "直接证据" in text


def test_report_marks_unconfirmed_environment(worlds):
    context, analysis, plan = _fixtures(worlds)
    plan.environment_confirmed = False
    plan.environment_version = ""
    record = _record(plan, [("TC-A", "passed")])
    text = render(plan, record, analysis=analysis, context=context)
    assert "未确认" in text


def test_report_marks_stub_mode(worlds):
    context, _, plan = _fixtures(worlds)
    analysis = ImpactAnalysis(summary="离线", model="stub")
    record = _record(plan, [("TC-A", "passed")])
    text = render(plan, record, analysis=analysis, context=context)
    assert "离线桩" in text


def test_report_without_context_degrades_honestly(worlds):
    _, _, plan = _fixtures(worlds)
    record = _record(plan, [("TC-A", "passed")])
    text = render(plan, record)
    assert "未提供采集上下文" in text


def test_write_report_creates_file(worlds, tmp_path):
    context, analysis, plan = _fixtures(worlds)
    record = _record(plan, [("TC-A", "passed")])
    path = write_report(
        render(plan, record, analysis=analysis, context=context), tmp_path / "r" / "report.md"
    )
    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith("# 回归报告")
