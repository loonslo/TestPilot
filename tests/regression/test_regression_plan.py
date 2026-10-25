from __future__ import annotations

import json

from api_test_agent.regression.collect import collect
from api_test_agent.regression.models import GapItem, ImpactAnalysis, ImpactItem
from api_test_agent.regression.plan import (
    load_plan,
    plan_fingerprint,
    plan_from_analysis,
    refresh_fingerprint,
    revalidate,
    save_plan,
)


def _analysis(**overrides) -> ImpactAnalysis:
    payload = {
        "summary": "改动",
        "impacts": [
            ImpactItem(
                id="IMP-01",
                change="库存校验",
                module="订单",
                evidence="direct",
                source_files=["src/orders/api.py"],
                existing_cases=["TC-A", "TC-B"],
            )
        ],
        "recommended_cases": ["TC-B", "TC-GHOST", "TC-UI"],
        "gaps": [GapItem(description="缺少并发下单用例", module="订单")],
        "uncertain": ["阈值可配置吗"],
    }
    payload.update(overrides)
    return ImpactAnalysis.model_validate(payload)


def test_smoke_set_is_always_kept_even_without_model_hits(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    plan = plan_from_analysis(
        run_id="r1",
        context=context,
        analysis=ImpactAnalysis(summary="模型没发现任何影响"),
        catalog=project.catalog,
        module_map=project.module_map,
    )
    ids = [e.case_id for e in plan.entries]
    assert "TC-A" in ids
    assert plan.entry("TC-A").priority == "smoke"
    assert plan.entry("TC-A").reason == "固定冒烟集（每轮必跑）"


def test_unknown_and_inactive_cases_are_rejected_not_expanded(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    plan = plan_from_analysis(
        run_id="r1",
        context=context,
        analysis=_analysis(),
        catalog=project.catalog,
        module_map=project.module_map,
    )
    rejected = {r.case_id: r.reason for r in plan.rejected_case_refs}
    assert "TC-GHOST" in rejected and "不存在" in rejected["TC-GHOST"]
    assert "TC-UI" in rejected and "ui" in rejected["TC-UI"]
    assert {e.case_id for e in plan.entries} == {"TC-A", "TC-B"}


def test_gaps_are_recorded_and_never_counted_as_pass(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    plan = plan_from_analysis(
        run_id="r1",
        context=context,
        analysis=_analysis(),
        catalog=project.catalog,
        module_map=project.module_map,
    )
    descriptions = [g.description for g in plan.uncovered_gaps]
    assert "缺少并发下单用例" in descriptions
    assert all(g.description not in {e.name for e in plan.entries} for g in plan.uncovered_gaps)


def test_fingerprint_detects_manual_edit_and_reconcile(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    plan = plan_from_analysis(
        run_id="r1",
        context=context,
        analysis=_analysis(),
        catalog=project.catalog,
        module_map=project.module_map,
    )
    assert plan_fingerprint(plan) == plan.plan_sha256
    plan.entries.append(
        plan.entries[0].model_copy(update={"case_id": "TC-C", "priority": "manual"})
    )
    assert plan_fingerprint(plan) != plan.plan_sha256
    refresh_fingerprint(plan)
    assert plan_fingerprint(plan) == plan.plan_sha256


def test_revalidate_detects_entry_drift(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    plan = plan_from_analysis(
        run_id="r1",
        context=context,
        analysis=_analysis(),
        catalog=project.catalog,
        module_map=project.module_map,
    )
    assert revalidate(plan, project.catalog) == []
    drifted = plan.model_copy(deep=True)
    drifted.entries[0].entry = ["repo/tests/test_orders.py::test_other"]
    problems = revalidate(drifted, project.catalog)
    assert problems and "测试入口" in problems[0]


def test_plan_roundtrip(worlds, tmp_path):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    plan = plan_from_analysis(
        run_id="r1",
        context=context,
        analysis=_analysis(),
        catalog=project.catalog,
        module_map=project.module_map,
    )
    path = save_plan(plan, tmp_path / "plan.json")
    loaded = load_plan(path)
    assert loaded.run_id == "r1"
    assert loaded.plan_sha256 == plan.plan_sha256
    # 落盘内容是 JSON，便于人工修改
    assert json.loads(path.read_text(encoding="utf-8"))["entries"]
