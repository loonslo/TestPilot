"""The single TestPilot CLI for API exploration and release regression."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from pydantic import ValidationError

from api_test_agent import __version__, benchmark, capability, checks, replay, runner, sandbox
from api_test_agent.budget import DailyLedger
from api_test_agent.envvars import read_env
from api_test_agent.layout import REFERENCE, Layout, SandboxError
from api_test_agent.model import ModelClient, ModelError
from api_test_agent.regression import cli as regression_cli
from api_test_agent.settings import Settings
from api_test_agent.tasks import cli as task_cli


def _model_client(settings: Settings) -> ModelClient:
    """The configured model with its key; the key never leaves this function's callers."""
    if settings.model is None:
        raise SandboxError("config has no [model] section")
    key = read_env(settings.model.api_key_env)
    if not key:
        raise SandboxError(
            f"{settings.model.api_key_env} is not set; set it as a user environment variable "
            "(it is read from the process environment or the user's Windows environment)"
        )
    return ModelClient(settings.model, key)


def _cmd_doctor(settings: Settings, _args: argparse.Namespace) -> int:
    results = checks.run_doctor(settings)
    for item in results:
        print(f"[{'ok' if item.ok else 'fail'}] {item.name}: {item.detail}")
    return 0 if all(item.ok for item in results) else 1


def _print_copy_report(report: sandbox.CopyReport) -> None:
    print(
        f"copy check: ok={report.ok} files={report.expected_files} tree={report.tree[:12]} "
        f"missing={len(report.missing)} modified={len(report.modified)} extra={len(report.extra)}"
    )
    for label, names in (
        ("missing", report.missing),
        ("modified", report.modified),
        ("extra", report.extra),
    ):
        for name in names[:10]:
            print(f"  {label}: {name}")


def _cmd_export(settings: Settings, args: argparse.Namespace) -> int:
    dest = sandbox.export_target(settings, force=args.force)
    print(f"exported {settings.target.commit[:12]} to {dest}")
    report = sandbox.verify_copy(settings)
    _print_copy_report(report)
    return 0 if report.ok else 1


def _cmd_verify(settings: Settings, _args: argparse.Namespace) -> int:
    report = sandbox.verify_copy(settings)
    _print_copy_report(report)
    return 0 if report.ok else 1


def _cmd_venv(settings: Settings, _args: argparse.Namespace) -> int:
    python = sandbox.ensure_venv(settings)
    print(f"target virtualenv ready: {python}")
    print(json.dumps(checks._target_versions(Layout.from_settings(settings)), indent=2))
    return 0


def _cmd_seed(settings: Settings, args: argparse.Namespace) -> int:
    meta = sandbox.build_seed(settings, args.seed_id, force=args.force)
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    return 0


def _cmd_check(settings: Settings, _args: argparse.Namespace) -> int:
    report = checks.run_sandbox_check(settings)
    for item in report["checks"]:
        print(f"[{'ok' if item['ok'] else 'fail'}] {item['name']}")
        if not item["ok"]:
            print(f"       {json.dumps(item['detail'], ensure_ascii=False)[:400]}")
    print(f"all_ok={report['all_ok']}")
    return 0 if report["all_ok"] else 1


def _cmd_benchmark_build(settings: Settings, args: argparse.Namespace) -> int:
    report = benchmark.build_all(settings, args.set, force=args.force)
    ok = True
    for item in report:
        if item["status"] == "exists":
            print(f"{item['id']}: already built (use --force to rebuild)")
            continue
        good = item["only_expected_files"] and item["imports_from_build"]
        ok &= good
        print(
            f"{item['id']}: built, changed {item['changed_files']}, "
            f"only expected files={item['only_expected_files']}, "
            f"imports from its own tree={item['imports_from_build']}"
        )
    return 0 if ok else 1


def _cmd_benchmark_conformance(settings: Settings, args: argparse.Namespace) -> int:
    results = benchmark.run_build(settings, args.build)
    for result in results:
        suffix = f"  {result.detail}" if result.detail else ""
        print(f"{result.rule_id}: {result.outcome}{suffix}")
    return 0 if all(r.outcome == "pass" for r in results) else 1


def _cmd_benchmark_verify(settings: Settings, args: argparse.Namespace) -> int:
    report = benchmark.verify_benchmark(settings, args.set, only=args.only)
    for item in report["builds"]:
        status = "ok" if item["ok"] else "FAIL"
        print(
            f"[{status}] {item['build']}: violated {item['violated'] or '-'} "
            f"(expected {item['expected_violations'] or '-'})"
        )
        for error in item["errors"]:
            print(f"       error in {error['rule']}: {error['detail'][:300]}")
    path = benchmark.write_report(settings, "benchmark-verify", report)
    print(f"all_ok={report['all_ok']}; report: {path.relative_to(settings.root)}")
    return 0 if report["all_ok"] else 1


def _cmd_model_status(settings: Settings, _args: argparse.Namespace) -> int:
    client = _model_client(settings)
    ledger = DailyLedger(Layout.from_settings(settings).runtime / "model-usage.json")
    print(f"model: {client.model_id} at {settings.model.base_url}")  # type: ignore[union-attr]
    print(f"requests counted by this tool today (UTC): {ledger.used()}")
    try:
        status = client.key_status()
    except ModelError as exc:
        print(f"could not read the account status: {exc}")
        return 1
    print(json.dumps(status, indent=2))
    return 0


def _cmd_model_ping(settings: Settings, _args: argparse.Namespace) -> int:
    client = _model_client(settings)
    ledger = DailyLedger(Layout.from_settings(settings).runtime / "model-usage.json")
    report = capability.check_tool_protocol(client, ledger)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


def _print_run(outcome: runner.RunOutcome) -> None:
    print(
        f"run {outcome.run_id}: {outcome.status} ({outcome.stop_reason}); "
        f"cases submitted {outcome.cases}; models that answered {outcome.served_by or 'none'}"
    )
    print(f"budget: {outcome.budget}")


def _cmd_agent_run(settings: Settings, args: argparse.Namespace) -> int:
    client = _model_client(settings)
    outcome = runner.run_task(
        settings, args.task, model=client, build=args.build, run_id=args.run_id
    )
    _print_run(outcome)
    print(f"artifacts: {outcome.run_dir.relative_to(settings.root)}")
    if args.replay and outcome.cases:
        result = replay.replay_run(settings, outcome.run_dir)
        for row in result["cases"]:
            print(f"  case {row['case_id']} ({row['rule_id']}): {row['label']}")
    return 0 if outcome.status != "aborted" else 1


def _cmd_agent_replay(settings: Settings, args: argparse.Namespace) -> int:
    run_dir = settings.root / "artifacts" / "runs" / args.run
    result = replay.replay_run(settings, run_dir)
    for row in result["cases"]:
        print(f"case {row['case_id']} ({row['rule_id']}): {row['label']}  {row['title']}")
    print(f"detects={result['detects']} false_positives={result['false_positives']}")
    return 0


def _cmd_agent_witnesses(settings: Settings, _args: argparse.Namespace) -> int:
    report = replay.verify_witnesses(settings)
    for row in report["witnesses"]:
        print(
            f"[{'ok' if row['ok'] else 'FAIL'}] {row['case_id']} for {row['defect']}: "
            f"also fails on {row['also_fails_on'] or '-'}"
        )
    path = benchmark.write_report(settings, "witnesses", report)
    print(f"all_ok={report['all_ok']}; report: {path.relative_to(settings.root)}")
    return 0 if report["all_ok"] else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testpilot", description="TestPilot · API 探索测试与发布回归"
    )
    parser.add_argument("--version", action="version", version=f"TestPilot {__version__}")
    parser.add_argument(
        "--config", type=Path, help="配置文件；探索默认 config.local.toml，回归默认 testagent.toml"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    regression_cli.register_commands(commands)
    task_cli.register_commands(commands)
    commands.add_parser(
        "doctor", help="check local prerequisites without calling any model"
    ).set_defaults(handler=_cmd_doctor)
    box = commands.add_parser("sandbox", help="manage the isolated target sandbox")
    actions = box.add_subparsers(dest="action", required=True)
    export = actions.add_parser("export", help="copy the pinned commit with git archive")
    export.add_argument("--force", action="store_true")
    export.set_defaults(handler=_cmd_export)
    actions.add_parser("verify", help="compare the copy with the pinned commit").set_defaults(
        handler=_cmd_verify
    )
    actions.add_parser(
        "venv", help="create the target's own virtualenv from its lock file"
    ).set_defaults(handler=_cmd_venv)
    seed = actions.add_parser("seed", help="build the seed database with accounts A and B")
    seed.add_argument("--seed-id", default=sandbox.DEFAULT_SEED)
    seed.add_argument("--force", action="store_true")
    seed.set_defaults(handler=_cmd_seed)
    actions.add_parser("check", help="run the A1 sandbox acceptance checks").set_defaults(
        handler=_cmd_check
    )
    suite = commands.add_parser("benchmark", help="the development benchmark (task A2)")
    steps = suite.add_subparsers(dest="action", required=True)
    build = steps.add_parser("build", help="create the defect builds next to the reference copy")
    build.add_argument("--force", action="store_true")
    build.add_argument("--set", choices=benchmark.SETS, default="defects")
    build.set_defaults(handler=_cmd_benchmark_build)
    conformance = steps.add_parser("conformance", help="run the 18 rule checks on one build")
    conformance.add_argument("--build", default=REFERENCE)
    conformance.set_defaults(handler=_cmd_benchmark_conformance)
    verify = steps.add_parser(
        "verify", help="label every build: the reference passes, each defect breaks only its rules"
    )
    verify.add_argument("--only", nargs="+", metavar="BUILD")
    verify.add_argument("--set", choices=benchmark.SETS, default="defects")
    verify.set_defaults(handler=_cmd_benchmark_verify)
    mdl = commands.add_parser("model", help="the configured model endpoint")
    mdl_steps = mdl.add_subparsers(dest="action", required=True)
    mdl_steps.add_parser("status", help="account facts and today's request count").set_defaults(
        handler=_cmd_model_status
    )
    mdl_steps.add_parser("ping", help="two requests: can the model call a tool?").set_defaults(
        handler=_cmd_model_ping
    )
    agent = commands.add_parser("agent", help="run the Agent on a task, or judge its cases")
    agent_steps = agent.add_subparsers(dest="action", required=True)
    run = agent_steps.add_parser("run", help="one Agent run on a dev task")
    run.add_argument("--task", required=True, help="for example dev-02")
    run.add_argument("--build", help="override the build the manifest assigns to the task")
    run.add_argument("--run-id")
    run.add_argument("--replay", action="store_true", help="judge the submitted cases afterwards")
    run.set_defaults(handler=_cmd_agent_run)
    again = agent_steps.add_parser("replay", help="judge the cases of a finished run")
    again.add_argument("--run", required=True, help="run id (a directory under artifacts/runs)")
    again.set_defaults(handler=_cmd_agent_replay)
    agent_steps.add_parser(
        "witnesses", help="replay the hand-written witness cases on every build"
    ).set_defaults(handler=_cmd_agent_witnesses)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)
    if hasattr(args, "task_handler"):
        return args.task_handler(args)
    if hasattr(args, "func"):
        return regression_cli.execute(args)
    config_path = args.config or Path("config.local.toml")
    try:
        settings = Settings.load(config_path)
    except FileNotFoundError:
        print(f"config not found: {config_path} (copy config.example.toml)", file=sys.stderr)
        return 2
    except (ValidationError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"invalid config {args.config}: {exc}", file=sys.stderr)
        return 2
    try:
        return args.handler(settings, args)
    except SandboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
