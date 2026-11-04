"""runner 模块：把计划里的用例编号解析为已登记的 pytest 入口并执行，收集真实结果。

边界：
- 不重新推断业务预期，不生成新脚本，只跑用例目录里登记过的 `entry`。
- 执行器由用例目录里显式的 runner 元数据决定，不靠 entry 里的关键字猜。
- 每条用例单独执行，失败/异常不重试（副作用不做盲目重试）；进程异常或超时记为 error，不计为通过。
- 结果只来自真实执行：没有执行到的用例写进 not_run，报告不会补写成通过。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api_test_agent.regression.models import (
    ROOT_PLACEHOLDER,
    CaseRunResult,
    RegressionPlan,
    RunRecord,
    TaskState,
)

ACCOUNTING_FLAG = "--accounting-json"
"""task 入口把结构化 accounting 写到这个参数指定的文件；runner 不解析它的文本输出。"""

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    accounting: dict[str, Any] | None = None
    env_note: dict[str, str] = field(default_factory=dict)


def _env(extra: dict[str, str]) -> dict[str, str]:
    """子进程环境：默认剔除名字里带 KEY/TOKEN 的变量。

    需要凭据的 task 入口不靠这里放行，而是由 `_credential_env` 按批准配置里的
    变量名白名单显式转发（见 `_case_env`），名单来自用例目录，值只来自父进程环境。
    """

    env = {
        k: v for k, v in os.environ.items() if "KEY" not in k.upper() and "TOKEN" not in k.upper()
    }
    env.update(extra)
    return env


def _executable(python: str | None) -> str:
    """解释器路径：已有的整条路径原样使用，否则取第一个词（如 "py -3"）。"""

    if not python:
        return sys.executable
    text = python.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if Path(text).exists():
        return text
    return text.split()[0]


def pytest_command(
    python: str | None, entry: list[str], repo: Path, extra_args: list[str] | None = None
) -> list[str]:
    """固定的运行方式：<配置的 python> -m pytest -q -p no:cacheprovider <固定参数> <登记入口>。"""

    return [
        _executable(python),
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *(extra_args or []),
        *entry,
    ]


RunnerCommandBuilder = Callable[[str | None, list[str], Path, list[str] | None], list[str]]


def task_command(
    python: str | None, entry: list[str], repo: Path, extra_args: list[str] | None = None
) -> list[str]:
    """固定的统一任务运行方式：<配置的 python> -m api_test_agent.cli task execute ...

    每条 entry 元素本身就是一个 argv token，这里不再二次拆词，否则含空格的路径
    （如 `C:/Users/Test User/tasks`）会被拆成两个参数。

    `extra_args` 是 suite 配置里给 pytest 的固定参数，不适用于 task 入口，这里忽略；
    task 自己的参数（--root/--case-id/--max-cases 等）写在 entry 里。
    """

    tokens = [t for t in entry]
    if tokens and tokens[0] == "testpilot":
        tokens = tokens[1:]
    # 用包入口 `-m api_test_agent`：`-m api_test_agent.cli` 只导入模块、不执行命令。
    return [_executable(python), "-m", "api_test_agent", *tokens]


RUNNER_REGISTRY: dict[str, RunnerCommandBuilder] = {
    "pytest": pytest_command,
    "task": task_command,
}


def register_runner(name: str, builder: RunnerCommandBuilder) -> None:
    RUNNER_REGISTRY[name] = builder


def get_runner(name: str) -> RunnerCommandBuilder:
    if name not in RUNNER_REGISTRY:
        raise ValueError(f"unknown test runner: {name}")
    return RUNNER_REGISTRY[name]


def entry_runner_name(entry: Any) -> str:
    """用例的执行器：显式声明的 runner 优先，其次是显式的任务绑定，最后才是 pytest。"""

    declared = getattr(entry, "runner", None)
    if declared:
        return str(declared)
    if getattr(entry, "task_binding", None):
        return "task"
    return "pytest"


def _credential_env(entry: Any) -> list[str]:
    """task 入口声明的凭据变量名白名单；只接受环境变量名形态，值不落盘。"""

    binding = getattr(entry, "task_binding", None) or {}
    names = binding.get("credential_env") or []
    return [name for name in names if isinstance(name, str) and _ENV_NAME.match(name)]


def _case_env(base: dict[str, str], entry: Any, runner_name: str) -> dict[str, str]:
    if runner_name != "task":
        return dict(base)
    forwarded = {name: os.environ[name] for name in _credential_env(entry) if name in os.environ}
    # 项目配置里的普通变量优先级更高；凭据只从父进程环境取，不写进配置。
    return {**forwarded, **base}


def _resolve_root_tokens(entry: list[str], task_root: Path | None) -> list[str]:
    if ROOT_PLACEHOLDER not in entry:
        return list(entry)
    if task_root is None:
        raise RunError(
            f"用例入口引用了任务根目录占位符 {ROOT_PLACEHOLDER}，但没有配置 project.task_root"
        )
    return [str(task_root) if token == ROOT_PLACEHOLDER else token for token in entry]


def _read_accounting(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return data if isinstance(data, dict) else None


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


def classify(
    returncode: int | None,
    output: str,
    timed_out: bool,
    accounting: dict[str, Any] | None = None,
) -> str:
    """把一次执行映射为结果。

    task 入口有结构化 accounting 时只认结构化数据（没有执行完就不是通过）；
    仅在没有结构化结果时退回退出码。pytest 入口按 pytest 语义：
    5=没有收集到用例属于配置问题，记为 error；退出码 0 但全部 skipped 也不算通过。
    """
    import re

    if accounting is not None:
        return _classify_accounting(accounting)
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


def _classify_accounting(accounting: dict[str, Any]) -> str:
    def count(key: str) -> int:
        value = accounting.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    planned = count("planned")
    if planned <= 0:
        return "error"
    if count("failed") > 0:
        return "failed"
    if count("error") > 0 or count("blocked") > 0 or not accounting.get("conserved", False):
        return "error"
    if count("not_run") > 0 or count("skipped") > 0:
        return "skipped"
    if count("passed") >= planned:
        return "passed"
    return "error"


def classify_task_exit_code(returncode: int | None, timed_out: bool) -> str:
    """没有结构化结果时的退路：只按退出码判断，绝不用文本里的数字当通过证据。"""

    if timed_out or returncode is None:
        return "error"
    if returncode == 0:
        return "passed"
    if returncode == 1:
        return "failed"
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
    runner: str | RunnerCommandBuilder | None = None,
    task_root: Path | None = None,
    invoke_fn=invoke,
    on_case=None,
) -> RunRecord:
    """按计划逐条执行。

    `runner`：None 表示由每条用例自己声明的 runner 决定（显式元数据优先）；
    传字符串表示强制使用该执行器（覆盖用例声明）；传可调用对象表示完全自定义命令构造。
    `task_root`：解析用例入口里 `{task_root}` 占位符的实际目录。
    """

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

    with tempfile.TemporaryDirectory(prefix="testpilot-run-") as scratch:
        for index, entry in enumerate(plan.entries):
            tokens = _resolve_root_tokens(entry.entry, task_root)
            if callable(runner):
                runner_name = "custom"
                command = runner(python, tokens, repo, extra_args)
            else:
                runner_name = runner or entry_runner_name(entry)
                command = get_runner(runner_name)(python, tokens, repo, extra_args)
            accounting_path = (
                Path(scratch) / f"{index:04d}-accounting.json" if runner_name == "task" else None
            )
            if accounting_path is not None:
                command = [*command, ACCOUNTING_FLAG, str(accounting_path)]
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
            result = invoke_fn(command, repo, _case_env(env, entry, runner_name), per_case_timeout)
            accounting = result.accounting
            if accounting_path is not None and accounting is None:
                accounting = _read_accounting(accounting_path)
            if accounting_path is not None:
                outcome = (
                    classify(result.returncode, result.output, result.timed_out, accounting)
                    if accounting is not None
                    else classify_task_exit_code(result.returncode, result.timed_out)
                )
            else:
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
                accounting=accounting,
                executed_at=datetime.now(UTC),
            )
            record.results.append(item)
            if result.error:
                record.errors.append(f"{entry.case_id}: {result.error}")
            if accounting_path is not None and accounting is None:
                record.errors.append(
                    f"{entry.case_id}: task 入口没有写出结构化结果，只能按退出码 "
                    f"{result.returncode} 判定"
                )
            if on_case:
                on_case(item)
    record.finished_at = datetime.now(UTC)
    record.recount()
    if dry_run:
        return record
    run_integrity_problem = any("无法启动测试进程" in e for e in record.errors)
    partially_executed = any(
        isinstance(item.accounting, dict) and int(item.accounting.get("not_run") or 0) > 0
        for item in record.results
    )
    state: TaskState
    if run_integrity_problem:
        state = "aborted"
    elif partially_executed:
        state = "incomplete"
    else:
        state = "completed"
    record.state = state
    return record


def planned_but_missing(plan: RegressionPlan, record: RunRecord) -> list[str]:
    done = {r.case_id for r in record.results}
    return [e.case_id for e in plan.entries if e.case_id not in done]
