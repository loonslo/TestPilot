"""runner 模块：把计划里的用例编号解析为已登记的 pytest 入口并执行，收集真实结果。

边界：
- 不重新推断业务预期，不生成新脚本，只跑用例目录里登记过的 `entry`。
- 每条用例单独执行，失败/异常不重试（副作用不做盲目重试）；进程异常或超时记为 error，不计为通过。
- 结果只来自真实执行：没有执行到的用例写进 not_run，报告不会补写成通过。
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from api_test_agent.regression.models import CaseRunResult, RegressionPlan, RunRecord, TaskState


class RunError(RuntimeError):
    """执行前置条件不满足；不产生部分结果。"""


@dataclass
class Invocation:
    command: list[str]
    output: str
    returncode: int | None
    duration_seconds: float
    timed_out: bool = False
    error: str = ""
    env_note: dict[str, str] = field(default_factory=dict)


def _env(extra: dict[str, str]) -> dict[str, str]:
    import os

    env = {
        k: v for k, v in os.environ.items() if "KEY" not in k.upper() and "TOKEN" not in k.upper()
    }
    env.update(extra)
    return env


def pytest_command(
    python: str | None, entry: list[str], repo: Path, extra_args: list[str] | None = None
) -> list[str]:
    """固定的运行方式：<配置的 python> -m pytest -q -p no:cacheprovider <固定参数> <登记入口>。"""

    executable = shlex.split(python)[0] if python else sys.executable
    return [executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *(extra_args or []), *entry]


RunnerCommandBuilder = Callable[[str | None, list[str], Path, list[str] | None], list[str]]

RUNNER_REGISTRY: dict[str, RunnerCommandBuilder] = {
    "pytest": pytest_command,
}


def register_runner(name: str, builder: RunnerCommandBuilder) -> None:
    RUNNER_REGISTRY[name] = builder


def get_runner(name: str) -> RunnerCommandBuilder:
    if name not in RUNNER_REGISTRY:
        raise ValueError(f"unknown test runner: {name}")
    return RUNNER_REGISTRY[name]


def invoke(command: list[str], cwd: Path, extra_env: dict[str, str], timeout: float) -> Invocation:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_env(extra_env),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):  # pragma: no cover - 兼容 bytes 模式
            partial = partial.decode("utf-8", "replace")
        return Invocation(
            command=command,
            output=partial,
            returncode=None,
            duration_seconds=time.monotonic() - started,
            timed_out=True,
            error=f"执行超时（>{timeout}s）",
        )
    except OSError as exc:
        return Invocation(
            command=command,
            output="",
            returncode=None,
            duration_seconds=time.monotonic() - started,
            error=f"无法启动测试进程: {exc}",
        )
    return Invocation(
        command=command,
        output=(proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else ""),
        returncode=proc.returncode,
        duration_seconds=time.monotonic() - started,
    )


def classify(returncode: int | None, output: str, timed_out: bool) -> str:
    """把 pytest 退出码映射为结果。5=没有收集到用例，属于配置问题，记为 error。
    若退出码为 0 但所有用例均为 skipped 或未收集到，不能误判为 passed。
    """
    import re

    if timed_out or returncode is None:
        return "error"
    if returncode == 5:
        return "error"
    if returncode == 1:
        return "failed"
    if returncode == 0:
        passed_matches = re.findall(r"\b(\d+)\s+passed\b", output)
        passed_count = sum(int(n) for n in passed_matches)
        skipped_matches = re.findall(r"\b(\d+)\s+skipped\b", output)
        skipped_count = sum(int(n) for n in skipped_matches)
        if skipped_count > 0 and passed_count == 0:
            return "skipped"
        if passed_count == 0 and ("no tests ran" in output or "collected 0 items" in output):
            return "error"
        return "passed"
    return "error"


def _assertions(output: str, limit: int = 6) -> list[str]:
    lines: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("E   ", "E ")):
            lines.append(stripped[:300])
        if len(lines) >= limit:
            break
    return lines


def _excerpt(output: str, limit: int = 4000) -> str:
    text = output.strip()
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...（中间省略）...\n" + text[-limit // 2 :]


def run_plan(
    plan: RegressionPlan,
    *,
    repo: Path,
    per_case_timeout: float = 600.0,
    python: str | None = None,
    extra_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    dry_run: bool = False,
    runner: str | RunnerCommandBuilder = "pytest",
    invoke_fn=invoke,
    on_case=None,
) -> RunRecord:
    if not plan.entries:
        raise RunError("计划里没有可执行用例；请先在编辑器中确认计划或调整用例目录")
    record = RunRecord(
        run_id=plan.run_id,
        project=plan.project,
        state="running",
        plan_sha256=plan.plan_sha256,
        base_sha=plan.base_sha,
        head_sha=plan.head_sha,
        environment_version=plan.environment_version,
        environment_confirmed=plan.environment_confirmed,
    )
    if dry_run:
        record.state = "planned"
        record.errors.append("dry-run：只解析命令，未实际执行用例")
    env = dict(extra_env or {})
    command_builder = runner if callable(runner) else get_runner(runner)
    for entry in plan.entries:
        command = command_builder(python, entry.entry, repo, extra_args)
        if dry_run:
            record.results.append(
                CaseRunResult(
                    case_id=entry.case_id,
                    name=entry.name,
                    entry=entry.entry,
                    priority=entry.priority,
                    result="skipped",
                    output_excerpt=f"dry-run: {' '.join(command)}",
                )
            )
            continue
        record.command = command
        result = invoke_fn(command, repo, env, per_case_timeout)
        outcome = classify(result.returncode, result.output, result.timed_out)
        item = CaseRunResult(
            case_id=entry.case_id,
            name=entry.name,
            entry=entry.entry,
            priority=entry.priority,
            result=outcome,  # type: ignore[arg-type]
            duration_seconds=round(result.duration_seconds, 3),
            assertions=_assertions(result.output),
            output_excerpt=_excerpt(result.output),
            executed_at=datetime.now(UTC),
        )
        record.results.append(item)
        if result.error:
            record.errors.append(f"{entry.case_id}: {result.error}")
        if on_case:
            on_case(item)
    record.finished_at = datetime.now(UTC)
    record.recount()
    if dry_run:
        return record
    run_integrity_problem = any("无法启动测试进程" in e for e in record.errors)
    record.state: TaskState = "aborted" if run_integrity_problem else "completed"
    return record


def planned_but_missing(plan: RegressionPlan, record: RunRecord) -> list[str]:
    done = {r.case_id for r in record.results}
    return [e.case_id for e in plan.entries if e.case_id not in done]
