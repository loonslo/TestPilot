"""两阶段命令行：plan 收集并生成建议，run 执行已确认的计划并出报告。

    testpilot plan --base <ref> --head <ref> [--config PATH] [--run-id ID] [--stub] [--no-ai]
    testpilot run  <run_id> [--env-version V] [--skip-confirm] [--dry-run]
    testpilot show <run_id> [--json]
    testpilot runs
    testpilot check [--config PATH]

退出码：0 完成；1 失败（计划/执行异常或无回归）；2 使用错误；3 需要人工确认而未确认（自动回退）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from api_test_agent.regression import __version__
from api_test_agent.regression.analyze import AnalysisError, AnalysisResult, Analyzer, StubAnalyzer
from api_test_agent.regression.collect import CollectError, collect, resolve_version, verify_commits
from api_test_agent.regression.models import (
    CollectionContext,
    ImpactAnalysis,
    RegressionPlan,
    RunRecord,
)
from api_test_agent.regression.plan import (
    load_plan,
    plan_fingerprint,
    plan_from_analysis,
    refresh_fingerprint,
    revalidate,
    save_plan,
)
from api_test_agent.regression.project import Project, ProjectError, find_config, load_project
from api_test_agent.regression.report import render, summarize, write_report
from api_test_agent.regression.runner import RunError, planned_but_missing, run_plan
from api_test_agent.regression.store import (
    append_log,
    list_runs,
    new_run_id,
    read_json,
    run_dir,
    write_json,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NEEDS_CONFIRM = 3


# --------------------------------------------------------------------------- 公共


def _project(args: argparse.Namespace) -> Project:
    config_path = find_config(Path(args.config) if args.config else None, None)
    return load_project(config_path)


def _load_context(directory: Path) -> CollectionContext | None:
    path = directory / "context.json"
    if not path.is_file():
        return None
    try:
        return CollectionContext.model_validate(read_json(path))
    except Exception:  # noqa: BLE001 - 上下文只用于报告展示，损坏时降级
        return None


def _load_analysis(directory: Path) -> ImpactAnalysis | None:
    path = directory / "analysis.json"
    if not path.is_file():
        return None
    try:
        return ImpactAnalysis.model_validate(read_json(path))
    except Exception:  # noqa: BLE001
        return None


def _load_record(directory: Path) -> RunRecord | None:
    path = directory / "run.json"
    if not path.is_file():
        return None
    try:
        return RunRecord.model_validate(read_json(path))
    except Exception:  # noqa: BLE001
        return None


def _print_plan_summary(plan: RegressionPlan, directory: Path) -> None:
    smoke = [e for e in plan.entries if e.priority == "smoke"]
    model = [e for e in plan.entries if e.priority == "model"]
    manual = [e for e in plan.entries if e.priority == "manual"]
    print("")
    print(f"环节 2/3  已生成计划（未执行任何测试）   run_id={plan.run_id}")
    print(f"  版本: {plan.base_sha[:12]} -> {plan.head_sha[:12]}")
    print(
        f"  用例: 共 {len(plan.entries)} 条（固定冒烟 {len(smoke)} / 建议+确认 {len(model)} / 人工 {len(manual)}）"
    )
    for entry in plan.entries:
        tag = {"smoke": "冒烟", "model": "建议", "manual": "人工"}[entry.priority]
        print(f"    [{tag}] {entry.case_id:<16} {entry.name}")
    if plan.rejected_case_refs:
        print(f"  被剔除的用例引用 {len(plan.rejected_case_refs)} 条：")
        for item in plan.rejected_case_refs:
            print(f"    - {item.case_id}: {item.reason}")
    if plan.uncovered_gaps:
        print(f"  覆盖缺口 {len(plan.uncovered_gaps)} 项（未覆盖，不计入通过数）")
    if plan.uncertain:
        print(f"  待确认 {len(plan.uncertain)} 项")
    print("")
    print("  下一步：")
    print(f"    1) 打开并核对计划: {directory / 'plan.json'}")
    print(f"    2) 如修改了用例（增删/调整优先级），先执行: testpilot reconcile {plan.run_id}")
    print(f"    3) 确认后执行: testpilot run {plan.run_id} --env-version <测试环境版本>")
    print(f"    跳步执行（仅限脚本/测试）: testpilot run {plan.run_id} --skip-confirm")


# --------------------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> int:
    project = _project(args)
    print(f"项目: {project.name}")
    print(f"仓库: {project.repo}")
    print(f"模块映射: {len(project.module_map.modules)} 个模块")
    print(
        f"用例目录: {len(project.catalog.cases)} 条（active {len(project.catalog.active())}），冒烟 {len(project.catalog.smoke)} 条"
    )
    problems = 0
    for doc in project.config.docs:
        path = project.root / doc
        state = "存在" if path.is_file() else "缺失"
        if not path.is_file():
            problems += 1
        print(f"资料: {doc} [{state}]")
    for warning in project.warnings:
        print(f"警告: {warning}")
    if project.config.model is None:
        print("模型: 未配置 [model]（只能使用 --stub 离线桩）")
    else:
        key = os.environ.get(project.config.model.api_key_env)
        print(
            f"模型: {project.config.model.model} @ {project.config.model.base_url} 密钥环境变量 {project.config.model.api_key_env}: {'已设置' if key else '未设置'}"
        )
    # 冒烟集与用例类型对照：UI/人工用例不应在冒烟集里
    by_id = project.cases_by_id()
    for case_id in project.catalog.smoke:
        case = by_id[case_id]
        if case.status != "active":
            print(f"错误: 冒烟用例 {case_id} 状态为 {case.status}")
            problems += 1
    print("")
    print("检查通过" if problems == 0 else f"发现 {problems} 个问题")
    return EXIT_OK if problems == 0 else EXIT_FAIL


# --------------------------------------------------------------------------- plan


def _analyze(
    project: Project,
    context: CollectionContext,
    directory: Path,
    *,
    use_stub: bool,
    no_ai: bool,
) -> AnalysisResult:
    prompt_path = directory / "prompt.txt"

    def on_prompt(text: str) -> Path:
        prompt_path.write_text(text, encoding="utf-8")
        return prompt_path

    if no_ai:
        analysis = ImpactAnalysis(
            summary="本次未调用模型（--no-ai）：只依据用例目录与模块映射生成保守计划。",
            uncertain=["未做影响分析，回归范围仅来自固定冒烟集与模块映射默认关联用例"],
            analyzed_scope=[],
            model="none",
        )
        return AnalysisResult(
            analysis=analysis, prompt_chars=0, response_chars=0, latency_seconds=0.0, attempts=0
        )

    analyzer = (
        StubAnalyzer(project)
        if use_stub
        else Analyzer(
            project.config.model,
            api_key=os.environ.get(project.config.model.api_key_env)
            if project.config.model
            else None,
        )
    )
    result = analyzer.analyze(context, project.prompts_dir, on_prompt=on_prompt)
    write_json(directory / "analysis.json", result.analysis)
    return result


def cmd_plan(args: argparse.Namespace) -> int:
    project = _project(args)
    if args.stub and args.no_ai:
        print("错误: --stub 与 --no-ai 不能同时使用", file=sys.stderr)
        return EXIT_USAGE
    if not args.stub and not args.no_ai and project.config.model is None:
        print("错误: 项目配置没有 [model] 段；离线演示请加 --stub", file=sys.stderr)
        return EXIT_USAGE

    if not args.stub and not args.no_ai:
        if not getattr(args, "allow_paid", False) and not os.environ.get(
            "TESTPILOT_ALLOW_PAID_MODEL"
        ):
            print(
                "错误: 付费模型调用守卫拦截：未获得用户明确批准。\n"
                "  调用商业模型产生费用（每次 <= 1 元人民币，总额度受控）。\n"
                "  离线分析请追加 --stub；如已确认批准付费调用，请追加 --allow-paid 参数或设置环境变量 TESTPILOT_ALLOW_PAID_MODEL=1",
                file=sys.stderr,
            )
            return EXIT_USAGE

    run_id = args.run_id or new_run_id()
    directory = run_dir(project.artifacts_dir, run_id)
    append_log(directory, f"plan 开始 base={args.base} head={args.head}")

    print(f"环节 1/3  采集版本变更   run_id={run_id}")
    try:
        context = collect(project, args.base, args.head, extra_excludes=args.exclude or [])
    except CollectError as exc:
        append_log(directory, f"采集失败: {exc}")
        print(f"采集失败: {exc}", file=sys.stderr)
        return EXIT_FAIL
    write_json(directory / "context.json", context)
    analyzed = len(context.analyzed_paths)
    print(
        f"  变更文件 {len(context.changes)} 个，送入分析 {analyzed} 个；diff {context.diff_included_chars}/{context.diff_total_chars} 字符"
    )
    for warning in context.warnings:
        print(f"  警告: {warning}")
    for omission in context.omissions[:10]:
        print(f"  未分析: {omission}")
    if len(context.omissions) > 10:
        print(f"  未分析: 另有 {len(context.omissions) - 10} 条，见 plan.json 的 omissions")

    print(
        "环节 2/3  生成影响分析与回归建议"
        + ("（离线桩）" if args.stub else "（未调用模型）" if args.no_ai else "")
    )
    try:
        result = _analyze(project, context, directory, use_stub=args.stub, no_ai=args.no_ai)
    except AnalysisError as exc:
        append_log(directory, f"分析失败: {exc}")
        print(f"分析失败: {exc}", file=sys.stderr)
        return EXIT_FAIL
    print(
        f"  影响条目 {len(result.analysis.impacts)}，建议用例 {len(result.analysis.recommended_cases)}，"
        f"新用例建议 {len(result.analysis.new_case_suggestions)}，缺口 {len(result.analysis.gaps)}"
    )

    plan = plan_from_analysis(
        run_id=run_id,
        context=context,
        analysis=result.analysis,
        catalog=project.catalog,
        module_map=project.module_map,
    )
    if args.env_version:
        plan.environment_version = args.env_version
        plan.environment_confirmed = True
    try:
        save_plan(plan, directory / "plan.json")
    except OSError as exc:
        print(f"无法写入计划: {exc}", file=sys.stderr)
        return EXIT_FAIL
    append_log(directory, f"plan 完成：{len(plan.entries)} 条用例，指纹 {plan.plan_sha256[:16]}")
    _print_plan_summary(plan, directory)
    return EXIT_OK


def cmd_reconcile(args: argparse.Namespace) -> int:
    project = _project(args)
    directory = project.artifacts_dir / args.run_id
    plan_path = directory / "plan.json"
    if not plan_path.is_file():
        print(f"错误: 找不到计划 {plan_path}", file=sys.stderr)
        return EXIT_USAGE
    plan = load_plan(plan_path)
    problems = revalidate(plan, project.catalog)
    for problem in problems:
        print(f"错误: {problem}", file=sys.stderr)
    if problems:
        return EXIT_FAIL
    old = plan.plan_sha256
    refresh_fingerprint(plan)
    save_plan(plan, plan_path)
    append_log(directory, f"人工修改计划后重新计算指纹 {old[:16]} -> {plan.plan_sha256[:16]}")
    print(f"已复核 {len(plan.entries)} 条用例入口，计划指纹已更新：{plan.plan_sha256[:16]}")
    return EXIT_OK


# --------------------------------------------------------------------------- run


def _needs_confirmation() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def cmd_run(args: argparse.Namespace) -> int:
    project = _project(args)
    directory = project.artifacts_dir / args.run_id
    plan_path = directory / "plan.json"
    if not plan_path.is_file():
        print(f"错误: 找不到计划 {plan_path}；请先执行 testpilot plan", file=sys.stderr)
        return EXIT_USAGE
    plan = load_plan(plan_path)

    # 1) 计划指纹：人工修改后必须 reconcile，否则拒绝执行
    if plan_fingerprint(plan) != plan.plan_sha256:
        print(
            "错误: 计划内容与指纹不一致（可能被手工修改）。请执行 "
            f"testpilot reconcile {args.run_id} 复核后再执行。",
            file=sys.stderr,
        )
        return EXIT_FAIL
    # 2) 用例目录复核
    problems = revalidate(plan, project.catalog)
    for problem in problems:
        print(f"错误: {problem}", file=sys.stderr)
    if problems:
        print(
            "请更新用例目录后重新执行 testpilot plan，或运行 testpilot reconcile 处理可修复项",
            file=sys.stderr,
        )
        return EXIT_FAIL
    # 3) 版本复核：端点提交仍存在，plan 之后 head 是否漂移
    try:
        verify_commits(project.repo, [plan.base_sha, plan.head_sha])
        head_now = resolve_version(project.repo, args.head or plan.head_ref)
    except CollectError as exc:
        print(f"错误: 版本复核失败: {exc}", file=sys.stderr)
        append_log(directory, f"run 中止：版本复核失败 {exc}")
        return EXIT_FAIL
    if head_now.sha != plan.head_sha:
        print(
            f"错误: 计划对应 {plan.head_sha[:12]}，当前 HEAD 已是 {head_now.sha[:12]}；"
            "代码已变化，需要重新 plan，不能沿用原结果",
            file=sys.stderr,
        )
        append_log(directory, "run 中止：head 漂移")
        return EXIT_FAIL

    # 4) 环境版本
    env_version = args.env_version or plan.environment_version
    env_confirmed = bool(args.env_version) or plan.environment_confirmed
    if not env_confirmed:
        if not getattr(args, "allow_unconfirmed_env", False):
            print(
                "错误: 未提供测试环境版本（--env-version）。无法核实环境时不得继续，"
                "或在确认风险后加 --allow-unconfirmed-env。",
                file=sys.stderr,
            )
            return EXIT_USAGE

    # 5) 两阶段确认
    if not args.skip_confirm:
        if not _needs_confirmation():
            print(
                "错误: 当前不是交互终端。两阶段确认要求人工确认计划；"
                "脚本场景请显式加 --skip-confirm（表示使用者已确认计划）。",
                file=sys.stderr,
            )
            return EXIT_NEEDS_CONFIRM
        _print_plan_summary(plan, directory)
        try:
            answer = input("确认执行以上用例？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消，没有执行任何用例。", file=sys.stderr)
            append_log(directory, "run 取消：使用者未确认")
            return EXIT_NEEDS_CONFIRM

    backup = None
    if (directory / "run.json").is_file():
        backup = directory / f"run.prev-{new_run_id()}.json"
        backup.write_text((directory / "run.json").read_text(encoding="utf-8"), encoding="utf-8")

    print(f"环节 3/3  执行 {len(plan.entries)} 条用例   {'(dry-run)' if args.dry_run else ''}")
    append_log(directory, f"run 开始：{len(plan.entries)} 条，dry_run={args.dry_run}")
    suite = project.config.suite
    record: RunRecord
    try:
        record = run_plan(
            plan,
            repo=project.repo,
            per_case_timeout=args.timeout or suite.timeout_seconds,
            python=args.python or (suite.python or None),
            extra_args=suite.extra_args,
            extra_env=project.config.environment,
            dry_run=args.dry_run,
            on_case=lambda item: print(
                f"    {item.case_id:<16} {item.result:<8} {item.duration_seconds:6.1f}s"
            ),
        )
    except RunError as exc:
        print(f"执行失败: {exc}", file=sys.stderr)
        append_log(directory, f"run 失败: {exc}")
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("\n已中断：已执行的结果不完整，本次标记为未完成。", file=sys.stderr)
        append_log(directory, "run 中断：KeyboardInterrupt")
        return EXIT_FAIL

    plan.environment_version = env_version
    plan.environment_confirmed = env_confirmed
    record.environment_version = env_version
    record.environment_confirmed = env_confirmed
    record.not_run = planned_but_missing(plan, record)
    if record.not_run:
        record.state = "incomplete"
    if args.dry_run:
        record.finished_at = record.finished_at
    write_json(directory / "run.json", record)
    save_plan(plan, directory / "plan.json")
    if backup is not None:
        print(f"  上一轮运行记录已另存: {backup.name}")

    report = render(
        plan,
        record,
        analysis=_load_analysis(directory),
        context=_load_context(directory),
    )
    report_path = write_report(report, directory / "report.md")
    stats = summarize(plan, record)
    append_log(
        directory,
        f"run 完成：state={record.state} 执行{stats['executed']} 通过{stats['passed']} "
        f"失败{stats['failed']} 异常{stats['error']} 缺口{stats['gaps']}",
    )
    print("")
    print(f"  报告: {report_path}")
    print(
        f"  结果: 执行 {stats['executed']}/{stats['planned']}，通过 {stats['passed']}，"
        f"失败 {stats['failed']}，异常 {stats['error']}，未覆盖 {stats['gaps']}"
    )
    if args.dry_run:
        return EXIT_OK
    return EXIT_OK if int(stats["failed"]) + int(stats["error"]) == 0 else EXIT_FAIL


# --------------------------------------------------------------------------- show / runs


def cmd_show(args: argparse.Namespace) -> int:
    project = _project(args)
    directory = project.artifacts_dir / args.run_id
    report_path = directory / "report.md"
    if not report_path.is_file():
        print(f"错误: 找不到报告 {report_path}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        plan = load_plan(directory / "plan.json")
        record = _load_record(directory) or RunRecord(run_id=plan.run_id, project=plan.project)
        import json

        print(json.dumps(summarize(plan, record), ensure_ascii=False, indent=2))
        return EXIT_OK
    print(report_path.read_text(encoding="utf-8"))
    return EXIT_OK


def cmd_runs(args: argparse.Namespace) -> int:
    project = _project(args)
    runs = list_runs(project.artifacts_dir)
    if not runs:
        print("还没有运行记录。")
        return EXIT_OK
    print(f"{'run_id':<18} {'状态':<8} {'计划':>4} {'执行':>4} {'通过':>4} {'失败':>4} {'异常':>4}")
    for path in runs:
        plan_path = path / "plan.json"
        if not plan_path.is_file():
            print(f"{path.name:<18} 无计划")
            continue
        plan = load_plan(plan_path)
        record = _load_record(path)
        if record is None:
            print(f"{path.name:<18} 已计划    {len(plan.entries):>4}    -    -    -    -")
            continue
        stats = summarize(plan, record)
        print(
            f"{path.name:<18} {str(record.state):<8} {stats['planned']:>4} {stats['executed']:>4} "
            f"{stats['passed']:>4} {stats['failed']:>4} {stats['error']:>4}"
        )
    return EXIT_OK


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testpilot",
        description="发布版本变更分析与回归建议（MVP）：plan 生成计划，run 执行并出报告。",
    )
    parser.add_argument("--version", action="version", version=f"TestPilot {__version__}")
    parser.add_argument("--config", help="项目配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    register_commands(sub)
    return parser


def register_commands(sub: argparse._SubParsersAction) -> None:
    """Register regression commands on the shared TestPilot command parser."""

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config",
            default=argparse.SUPPRESS,
            help="项目配置文件（默认在当前目录找 testagent.toml）",
        )

    p_plan = sub.add_parser("plan", help="采集 base..head 变更并生成回归计划（不执行测试）")
    common(p_plan)
    p_plan.add_argument("--base", required=True, help="上次发布版本（提交、标签或分支）")
    p_plan.add_argument("--head", required=True, help="本次候选版本（提交、标签或分支）")
    p_plan.add_argument("--run-id", help="自定义 run_id（默认按时间生成）")
    p_plan.add_argument("--env-version", help="测试环境版本号（可稍后 run 时补）")
    p_plan.add_argument("--stub", action="store_true", help="离线桩分析，不调用模型")
    p_plan.add_argument("--no-ai", action="store_true", help="完全不分析，只用冒烟集与模块映射")
    p_plan.add_argument("--exclude", action="append", help="额外排除的路径正则，可重复")
    p_plan.add_argument(
        "--allow-paid",
        action="store_true",
        help="显式批准调用付费模型进行影响分析（单次不超过 1 元）",
    )
    p_plan.set_defaults(func=cmd_plan)

    p_rec = sub.add_parser("reconcile", help="人工修改计划后重新校验并更新指纹")
    common(p_rec)
    p_rec.add_argument("run_id")
    p_rec.set_defaults(func=cmd_reconcile)

    p_run = sub.add_parser("run", help="执行已确认的计划并生成报告")
    common(p_run)
    p_run.add_argument("run_id")
    p_run.add_argument("--env-version", help="测试环境版本号")
    p_run.add_argument(
        "--allow-unconfirmed-env",
        action="store_true",
        help="允许环境版本未确认就执行（报告会标注）",
    )
    p_run.add_argument("--skip-confirm", action="store_true", help="跳过交互确认（脚本/测试用）")
    p_run.add_argument("--dry-run", action="store_true", help="只解析测试命令，不真正执行")
    p_run.add_argument("--head", help="复核时使用的 head 引用（默认沿用计划里的 head 引用）")
    p_run.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="单条用例超时秒数（默认取配置 suite.timeout_seconds）",
    )
    p_run.add_argument(
        "--python", help="执行测试的 Python（默认取配置 suite.python，再退回当前解释器）"
    )
    p_run.set_defaults(func=cmd_run)

    p_show = sub.add_parser("show", help="查看某次报告")
    common(p_show)
    p_show.add_argument("run_id")
    p_show.add_argument("--json", action="store_true", help="只输出汇总 JSON")
    p_show.set_defaults(func=cmd_show)

    p_runs = sub.add_parser("runs", help="列出历史运行")
    common(p_runs)
    p_runs.set_defaults(func=cmd_runs)

    p_check = sub.add_parser("check", help="检查配置、资产与模型密钥是否就绪")
    common(p_check)
    p_check.set_defaults(func=cmd_check)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return execute(args)


def execute(args: argparse.Namespace) -> int:
    """Execute a parsed regression command without loading exploration settings."""
    try:
        return int(args.func(args))
    except ProjectError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CollectError as exc:
        print(f"采集错误: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except AnalysisError as exc:
        print(f"分析错误: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
