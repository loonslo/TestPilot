"""plan 模块：把模型建议变成可核对、可修改的回归计划。

规则（方案第 6、7 节）：
- 固定冒烟集永远保留，不因模型"没发现相关变化"而被跳过。
- 模型只能建议已有用例编号；不存在的编号、非 active 用例一律剔除并记录原因。
- 未覆盖项（缺用例、UI/人工、材料不足）单独记录，不计入执行数。
- 计划落盘为 JSON 并带指纹；执行前若版本端点、用例入口或计划内容变化，指纹不匹配即拒绝续用。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from api_test_agent.regression.models import (
    CaseCatalog,
    CasePriority,
    CollectionContext,
    GapItem,
    ImpactAnalysis,
    ModuleMap,
    PlanEntry,
    RegressionPlan,
    RejectedCaseRef,
    TestCase,
)

# 这些排除原因属于"按规则主动排除"，不构成未分析缺口
_ACTIVE_EXCLUSION_PREFIXES = ("默认排除", "自定义排除")


def plan_from_analysis(
    *,
    run_id: str,
    context: CollectionContext,
    analysis: ImpactAnalysis,
    catalog: CaseCatalog,
    module_map: ModuleMap,
) -> RegressionPlan:
    entries: dict[str, PlanEntry] = {}
    rejected: list[RejectedCaseRef] = []

    def try_add(case_id: str, priority: CasePriority, reason: str) -> None:
        case = catalog.get(case_id)
        if case is None:
            rejected.append(RejectedCaseRef(case_id=case_id, reason="用例目录中不存在该编号"))
            return
        if case.status != "active":
            rejected.append(
                RejectedCaseRef(case_id=case_id, reason=f"用例状态为 {case.status}，本次不执行")
            )
            return
        existing = entries.get(case_id)
        if existing is None:
            entries[case_id] = PlanEntry.from_case(case, priority, reason)
        elif priority == "smoke" and existing.priority != "smoke":
            existing.priority = "smoke"
            existing.reason = reason

    # 1) 固定冒烟集始终保留
    for case_id in catalog.smoke:
        try_add(case_id, "smoke", "固定冒烟集（每轮必跑）")

    # 2) 模型建议 + 影响条目引用的用例（去重保序）
    recommended: list[str] = list(dict.fromkeys(analysis.recommended_cases))
    for item in analysis.impacts:
        for case_id in item.existing_cases:
            if case_id not in recommended:
                recommended.append(case_id)
    for case_id in recommended:
        try_add(case_id, "model", _why(case_id, analysis))

    # 3) 模块映射的默认关联用例兜底，避免模型整块漏掉某模块
    for change in context.changes:
        if change.excluded:
            continue
        for module_entry in module_map.by_path(change.path):
            for case_id in module_entry.cases:
                if case_id in entries:
                    continue
                try_add(case_id, "model", f"模块映射 {module_entry.module} 的默认关联用例")

    ordered = _order(entries, catalog)

    gaps: list[GapItem] = list(analysis.gaps)
    for change in context.changes:
        if change.excluded and not change.exclusion_reason.startswith(_ACTIVE_EXCLUSION_PREFIXES):
            gaps.append(
                GapItem(
                    description=f"{change.path} 未分析：{change.exclusion_reason}",
                    module=change.modules[0] if change.modules else "",
                    reason="insufficient_context",
                )
            )
    for module in _uncovered_modules(context, catalog, entries):
        gaps.append(
            GapItem(
                description=f"模块 {module} 有代码变更，但没有被纳入回归计划的可执行用例",
                module=module,
                reason="no_case",
            )
        )

    plan = RegressionPlan(
        run_id=run_id,
        project=context.project,
        base_sha=context.base_sha,
        head_sha=context.head_sha,
        base_ref=context.base_ref,
        head_ref=context.head_ref,
        entries=ordered,
        rejected_case_refs=_dedupe_rejected(rejected),
        uncovered_gaps=_dedupe_gaps(gaps),
        uncertain=list(analysis.uncertain),
        new_case_drafts=list(analysis.new_case_suggestions),
        omissions=list(context.omissions),
        environment_confirmed=False,
    )
    plan.plan_sha256 = plan_fingerprint(plan)
    return plan


def _why(case_id: str, analysis: ImpactAnalysis) -> str:
    reasons: list[str] = []
    for item in analysis.impacts:
        if case_id in item.existing_cases:
            reasons.append(f"{item.id} {item.change}")
    if case_id in analysis.recommended_cases:
        reasons.append("模型建议回归")
    text = "；".join(dict.fromkeys(reasons))
    return text[:300]


def _order(entries: dict[str, PlanEntry], catalog: CaseCatalog) -> list[PlanEntry]:
    rank = {c.id: i for i, c in enumerate(catalog.cases)}
    priority_rank = {"smoke": 0, "model": 1, "manual": 2}
    return sorted(
        entries.values(), key=lambda e: (priority_rank[e.priority], rank.get(e.case_id, 10_000))
    )


def _uncovered_modules(
    context: CollectionContext, catalog: CaseCatalog, entries: dict[str, PlanEntry]
) -> list[str]:
    planned_modules = {
        catalog.get(e.case_id).module
        for e in entries.values()
        if catalog.get(e.case_id) is not None
    }
    modules: list[str] = []
    for change in context.changes:
        if change.excluded:
            continue
        for module in change.modules:
            if module in modules:
                continue
            if module not in planned_modules:
                modules.append(module)
    return modules


def _dedupe_rejected(items: list[RejectedCaseRef]) -> list[RejectedCaseRef]:
    seen: dict[str, RejectedCaseRef] = {}
    for item in items:
        seen.setdefault(item.case_id, item)
    return list(seen.values())


def _dedupe_gaps(items: list[GapItem]) -> list[GapItem]:
    seen: dict[str, GapItem] = {}
    for item in items:
        seen.setdefault(item.description, item)
    return list(seen.values())


def plan_fingerprint(plan: RegressionPlan) -> str:
    """计划指纹：版本端点 + 每条用例的入口与优先级。"""

    payload = {
        "base_sha": plan.base_sha,
        "head_sha": plan.head_sha,
        "entries": [
            {"case_id": e.case_id, "entry": e.entry, "priority": e.priority} for e in plan.entries
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def refresh_fingerprint(plan: RegressionPlan) -> RegressionPlan:
    plan.plan_sha256 = plan_fingerprint(plan)
    return plan


def save_plan(plan: RegressionPlan, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_plan(path: Path) -> RegressionPlan:
    return RegressionPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))


def revalidate(plan: RegressionPlan, catalog: CaseCatalog) -> list[str]:
    """执行前用当前用例目录复核计划：用例是否仍存在、入口是否漂移、状态是否仍可执行。"""

    problems: list[str] = []
    for entry in plan.entries:
        case: TestCase | None = catalog.get(entry.case_id)
        if case is None:
            problems.append(f"{entry.case_id}: 用例目录中已不存在")
        elif list(case.entry) != list(entry.entry):
            problems.append(
                f"{entry.case_id}: 测试入口与计划不一致（计划 {entry.entry}，目录 {case.entry}）"
            )
        elif case.status != "active":
            problems.append(f"{entry.case_id}: 用例状态已变为 {case.status}")
    return problems
