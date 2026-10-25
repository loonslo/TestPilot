"""report 模块：基于计划和真实运行记录生成 Markdown 报告。

原则：不编造缺失的执行结果。未执行的用例显示"未执行"，未覆盖项独立列出且不计入通过数。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from api_test_agent.regression.models import (
    CaseRunResult,
    CollectionContext,
    ImpactAnalysis,
    RegressionPlan,
    RunRecord,
)

EVIDENCE_LABEL = {
    "direct": "直接证据",
    "inferred": "关联推断",
    "uncertain": "待确认",
}

RESULT_LABEL = {
    "passed": "通过",
    "failed": "失败",
    "error": "异常",
    "skipped": "未执行",
}

STATE_LABEL = {
    "planned": "已生成计划",
    "running": "执行中",
    "completed": "已完成",
    "aborted": "执行异常",
    "incomplete": "未完成",
}


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def _pct(part: int, total: int) -> str:
    if total <= 0:
        return "-"
    return f"{part * 100 / total:.0f}%"


def summarize(plan: RegressionPlan, record: RunRecord) -> dict[str, object]:
    totals = dict(record.totals)
    executed = int(totals.get("executed", 0))
    passed = int(totals.get("passed", 0))
    planned = len(plan.entries)
    smoke_total = sum(1 for e in plan.entries if e.priority == "smoke")
    smoke_passed = sum(1 for r in record.results if r.priority == "smoke" and r.result == "passed")
    return {
        "planned": planned,
        "executed": executed,
        "passed": passed,
        "failed": int(totals.get("failed", 0)),
        "error": int(totals.get("error", 0)),
        "skipped": int(totals.get("skipped", 0)),
        "not_run": len(plan.entries) - executed,
        "pass_rate": _pct(passed, executed),
        "smoke_total": smoke_total,
        "smoke_passed": smoke_passed,
        "gaps": len(plan.uncovered_gaps),
        "state": record.state,
    }


def render(
    plan: RegressionPlan,
    record: RunRecord,
    *,
    analysis: ImpactAnalysis | None = None,
    context: CollectionContext | None = None,
    generated_at: datetime | None = None,
) -> str:
    now = generated_at or datetime.now(UTC)
    stats = summarize(plan, record)
    lines: list[str] = []
    add = lines.append

    add(f"# 回归报告 {plan.project} · {plan.run_id}")
    add("")
    add(
        f"> 生成时间：{now.astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}　任务状态：**{STATE_LABEL.get(record.state, record.state)}**"
    )
    add("")
    add("## 0. 本次范围与版本")
    add("")
    add("| 项目 | 值 |")
    add("| --- | --- |")
    add(f"| 项目 | {plan.project} |")
    add(f"| 上次发布（base） | `{plan.base_sha}`（{plan.base_ref}） |")
    add(f"| 本次候选（head） | `{plan.head_sha}`（{plan.head_ref}） |")
    if context is not None:
        add(f"| 采集方式 | 发布版本两端快照，共同祖先 `{context.merge_base[:12]}` |")
        add(
            f"| 变更文件 | {len(context.changes)} 个（分析 {len(context.analyzed_paths)}，排除/未分析 {len(context.changes) - len(context.analyzed_paths)}） |"
        )
    env = plan.environment_version or "未填写"
    add(
        f"| 测试环境版本 | {env}（{'已确认' if plan.environment_confirmed else '未确认，报告中结论需复核'}） |"
    )
    add(f"| 计划指纹 | `{plan.plan_sha256[:16]}` |")
    add("")

    add("## 1. 结论摘要")
    add("")
    add(
        f"- 计划执行用例 **{stats['planned']}** 条，实际执行 **{stats['executed']}** 条："
        f"通过 {stats['passed']}、失败 {stats['failed']}、异常 {stats['error']}。"
    )
    add(f"- 执行通过率 **{stats['pass_rate']}**（只统计实际执行的用例；异常不计为通过）。")
    add(f"- 固定冒烟集 {stats['smoke_total']} 条，通过 {stats['smoke_passed']} 条。")
    add(f"- 未覆盖项 **{stats['gaps']}** 项，没有对应自动化，**不计入通过数**。")
    if stats["not_run"]:
        add(f"- 计划中 {stats['not_run']} 条用例未实际执行，报告中标记为“未执行”。")
    if record.errors:
        add(f"- 执行异常记录 {len(record.errors)} 条，详见第 5 节。")
    add("")

    add("## 2. 改了什么")
    add("")
    if context is None:
        add("_未提供采集上下文（plan 与 run 之间可能被拆分），本节仅显示计划信息。_")
        add("")
    else:
        changes = context.changes
        add(
            f"`{context.base_sha[:12]}` → `{context.head_sha[:12]}`：{context.subject or '（无提交说明）'}"
        )
        add("")
        add("| 文件 | 状态 | 增/删 | 业务模块 | 是否分析 |")
        add("| --- | --- | --- | --- | --- |")
        for change in changes[:200]:
            modules = "、".join(change.modules) if change.modules else "未映射"
            analyzed = "否（" + change.exclusion_reason + "）" if change.excluded else "是"
            status = {"A": "新增", "M": "修改", "D": "删除", "R": "重命名", "C": "复制"}.get(
                change.status, change.status
            )
            add(
                f"| `{change.path}` | {status} | +{change.added}/-{change.deleted} | {modules} | {analyzed} |"
            )
        if len(changes) > 200:
            add(f"| _其余 {len(changes) - 200} 个文件省略_ | | | | |")
        add("")

    add("## 3. 影响分析与其依据")
    add("")
    if analysis is None:
        add("_本次报告没有附带模型分析（离线桩或分析结果未加载）。_")
        add("")
    else:
        if analysis.summary:
            add(analysis.summary)
            add("")
        if analysis.model:
            add(f"分析模型：`{analysis.model}`")
            add("")
        if not analysis.impacts:
            add("_模型没有给出任何影响条目；这不代表系统无缺陷，请结合 diff 人工确认。_")
            add("")
        for item in analysis.impacts:
            add(f"### {item.id} {item.change}")
            add("")
            add(f"- 业务模块：{item.module}")
            add(f"- 证据强度：**{EVIDENCE_LABEL.get(item.evidence, item.evidence)}**")
            if item.source_files:
                add(f"- 代码位置：{'、'.join(f'`{p}`' for p in item.source_files)}")
            if item.source_lines:
                add(f"- 行号：{'、'.join(f'`{p}`' for p in item.source_lines)}")
            if item.business_source:
                add(f"- 业务来源：{'、'.join(item.business_source)}")
            add(f"- 相关用例：{'、'.join(item.existing_cases) if item.existing_cases else '无'}")
            if item.note:
                add(f"- 说明：{item.note}")
            add("")

    add("## 4. 实际执行结果")
    add("")
    add("| 编号 | 用例 | 模块 | 来源 | 结果 | 耗时 |")
    add("| --- | --- | --- | --- | --- | --- |")
    priority_label = {"smoke": "固定冒烟", "model": "建议+确认", "manual": "人工追加"}
    results = {r.case_id: r for r in record.results}
    for entry in plan.entries:
        result: CaseRunResult | None = results.get(entry.case_id)
        if result is None:
            add(
                f"| {entry.case_id} | {entry.name} | {entry.module} | {priority_label[entry.priority]} | 未执行 | - |"
            )
        else:
            add(
                f"| {entry.case_id} | {entry.name} | {entry.module} | {priority_label[entry.priority]} | "
                f"{RESULT_LABEL.get(result.result, result.result)} | {_fmt_duration(result.duration_seconds)} |"
            )
    add("")

    failures = [r for r in record.results if r.result in ("failed", "error")]
    add("## 5. 失败与异常证据")
    add("")
    if not failures and not record.errors:
        add("本次没有失败或异常记录。注意：这只能说明已执行用例通过，不能证明系统无缺陷。")
        add("")
    for result in failures:
        add(f"### {result.case_id} · {RESULT_LABEL.get(result.result, result.result)}")
        add("")
        add(f"- 测试入口：`{' '.join(result.entry)}`")
        add(
            f"- 耗时：{_fmt_duration(result.duration_seconds)}；执行时间：{result.executed_at.isoformat() if result.executed_at else '未知'}"
        )
        if result.assertions:
            add("- 失败断言：")
            for line in result.assertions:
                add(f"  - `{line}`")
        if result.output_excerpt:
            add("")
            add("<details><summary>输出摘录</summary>")
            add("")
            add("```text")
            add(result.output_excerpt)
            add("```")
            add("")
            add("</details>")
        add("")
    if record.errors:
        add("### 运行器异常记录")
        add("")
        for error in record.errors:
            add(f"- {error}")
        add("")

    add("## 6. 建议回归但被剔除的用例")
    add("")
    if not plan.rejected_case_refs:
        add("无。")
    else:
        add("| 编号 | 剔除原因 |")
        add("| --- | --- |")
        for item in plan.rejected_case_refs:
            add(f"| {item.case_id} | {item.reason} |")
    add("")

    add("## 7. 覆盖缺口（未覆盖，不计入通过数）")
    add("")
    if not plan.uncovered_gaps:
        add("无记录。")
    else:
        reason_label = {
            "no_case": "无对应用例",
            "ui_or_manual": "需 UI/人工执行",
            "external": "依赖仓库外或外部服务",
            "insufficient_context": "材料不足",
            "other": "其他",
        }
        add("| 缺失内容 | 模块 | 类型 |")
        add("| --- | --- | --- |")
        for gap in plan.uncovered_gaps:
            add(
                f"| {gap.description} | {gap.module or '-'} | {reason_label.get(gap.reason, gap.reason)} |"
            )
    add("")

    add("## 8. 新用例建议（草稿，需人工决定是否实现）")
    add("")
    if not plan.new_case_drafts:
        add("无。")
    else:
        for draft in plan.new_case_drafts:
            add(f"- **{draft.title}**（模块：{draft.module}）")
            if draft.rationale:
                add(f"  - 理由：{draft.rationale}")
            if draft.related_impact:
                add(f"  - 关联影响：{draft.related_impact}")
            if draft.covered_by_existing:
                add(f"  - 相关已有用例：{'、'.join(draft.covered_by_existing)}")
    add("")

    add("## 9. 待确认事项")
    add("")
    uncertain = list(plan.uncertain)
    if not uncertain:
        add("无。")
    else:
        for text in uncertain:
            add(f"- {text}")
    add("")

    add("## 10. 本次未分析的部分")
    add("")
    if not plan.omissions:
        add("无记录。")
    else:
        for text in plan.omissions:
            add(f"- {text}")
    add("")

    add("## 11. 限制说明")
    add("")
    add("- 模型建议的用例只作为回归建议，固定冒烟集始终保留；模型选错/漏选属于预期风险。")
    add("- diff 分析不能保证发现全部影响，也不能证明系统无缺陷。")
    add("- 只有第 4 节“通过”的行才代表实际执行通过；“未执行”“异常”都不算通过。")
    if analysis is not None and analysis.model == "stub":
        add("- 本次使用**离线桩分析**（未调用模型），影响分析与用例建议仅用于链路验证。")
    add("")
    return "\n".join(lines)


def write_report(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
