"""The Agent loop: model, tools, observations, with budget, pacing and honest stop reasons.

The loop is a plain ``while``: check the budget, call the model, record its message, run its tool
calls one after the other and answer each, repeat. A text answer without tool calls is the final
report. Everything that ends a run says why: the stop reason is part of the result, a budget stop
is never dressed up as a completed task.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import openai

from api_test_agent import sandbox
from api_test_agent.budget import Budget, DailyLedger, Limits
from api_test_agent.layout import Layout, SandboxError, validate_id
from api_test_agent.model import ModelError, ModelReply
from api_test_agent.prompts import SYSTEM_PROMPT, first_message
from api_test_agent.settings import Settings
from api_test_agent.spec import Spec
from api_test_agent.targethttp import TargetHttp
from api_test_agent.tools import TaskScope, ToolBox
from api_test_agent.trace import Trace

FINAL_NOTICE = (
    "Budget notice: this is your last model call. Do not call any tool. Reply now with your final "
    "report in plain text: for each rule, what you checked and whether you found a problem."
)
MIN_TOOL_CALLS_FOR_COMPLETE = 3
SAME_CALL_LIMIT = 3
_ABORTING = {
    "model_quota",
    "model_payment",
    "model_auth",
    "model_policy",
    "model_bad_request",
    "model_protocol",
    "model_failures",
    "harness_error",
}


class ChatModel(Protocol):
    @property
    def model_id(self) -> str: ...

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        tool_choice: Literal["auto", "none"] = "auto",
    ) -> ModelReply: ...


@dataclass
class RunOutcome:
    run_id: str
    run_dir: Path
    task_id: str
    build: str
    status: Literal["completed", "partial", "aborted"]
    stop_reason: str
    cases: int
    budget: dict[str, Any]
    served_by: dict[str, int]
    final_report: str = ""
    events: list[str] = field(default_factory=list)


def dev_manifest(settings: Settings) -> dict[str, Any]:
    return json.loads((settings.root / "benchmarks" / "dev" / "manifest.json").read_text("utf-8"))


def build_of_task(settings: Settings, task_id: str) -> str:
    for task in dev_manifest(settings)["tasks"]:
        if task["id"] == task_id:
            return task["build"]
    raise SandboxError(f"task {task_id!r} is not in the dev manifest")


def run_task(
    settings: Settings,
    task_id: str,
    *,
    model: ChatModel,
    build: str | None = None,
    run_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    ledger: DailyLedger | None = None,
) -> RunOutcome:
    layout = Layout.from_settings(settings)
    public = settings.root / "benchmarks" / "public"
    scope = TaskScope.load(public / "tasks" / f"{validate_id(task_id, 'task id')}.json")
    spec = Spec.load(public)
    chosen_build = build or build_of_task(settings, task_id)
    run_id = validate_id(run_id or f"{task_id}-{datetime.now(UTC):%m%d-%H%M%S}", "run id")
    run_dir = settings.root / "artifacts" / "runs" / run_id
    if run_dir.exists():
        raise SandboxError(f"run {run_id!r} already exists")
    run_dir.mkdir(parents=True)
    limits = Limits.for_task(settings.budget, scope.limits)
    budget = Budget(
        limits=limits,
        daily=ledger or DailyLedger(layout.runtime / "model-usage.json"),
        max_requests_per_day=settings.budget.max_requests_per_day,
        clock=clock,
    )
    trace = Trace(run_dir / "trace.jsonl")
    started = datetime.now(UTC)
    scenario = sandbox.start_scenario(settings, f"run-{run_id}"[:40], build=chosen_build)
    http = TargetHttp(scenario.base_url, scenario.tokens, spec)
    toolbox = ToolBox(scope, spec, http, budget, run_dir)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": first_message(scope, spec, limits)},
    ]
    served: Counter[str] = Counter()
    final_report = ""
    stop_reason = "unknown"
    trace.event(
        "run_start",
        run_id=run_id,
        task_id=task_id,
        model_id=model.model_id,
        limits=limits.__dict__,
    )
    try:
        stop_reason, final_report = _loop(
            settings, model, toolbox, budget, messages, served, trace, sleep, clock
        )
    except Exception as exc:  # a bug in the harness must not look like a model result
        stop_reason = "harness_error"
        trace.event("harness_error", error=f"{type(exc).__name__}: {exc}")
        print(f"harness error: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        http.close()
        sandbox.stop_scenario(settings, scenario)
    if stop_reason == "final_report":
        complete = toolbox.calls_made >= MIN_TOOL_CALLS_FOR_COMPLETE and final_report.strip()
        status: Literal["completed", "partial", "aborted"] = "completed" if complete else "partial"
    elif stop_reason in _ABORTING:
        status = "aborted"
    else:
        status = "partial"
    outcome = RunOutcome(
        run_id=run_id,
        run_dir=run_dir,
        task_id=task_id,
        build=chosen_build,
        status=status,
        stop_reason=stop_reason,
        cases=len(toolbox.cases),
        budget=budget.snapshot(),
        served_by=dict(served),
        final_report=final_report,
    )
    trace.event("run_end", status=status, stop_reason=stop_reason, budget=outcome.budget)
    trace.close()
    _write_artifacts(settings, outcome, scope, started, model.model_id)
    return outcome


def _loop(
    settings: Settings,
    model: ChatModel,
    toolbox: ToolBox,
    budget: Budget,
    messages: list[dict[str, Any]],
    served: Counter[str],
    trace: Trace,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[str, str]:
    failures = 0
    last_signature, streak = "", 0
    last_call_at: float | None = None
    gap = settings.budget.min_seconds_between_model_calls
    while True:
        refusal = budget.model_call_refusal()
        if refusal:
            trace.event("budget_stop", reason=refusal)
            return refusal, ""
        last_call = budget.limits.max_model_calls - budget.model_calls == 1
        if last_call:
            messages.append({"role": "user", "content": FINAL_NOTICE})
        if last_call_at is not None and clock() - last_call_at < gap:
            sleep(gap - (clock() - last_call_at))
        budget.count_model_call()
        trace.event(
            "model_request", call=budget.model_calls, messages=len(messages), last_call=last_call
        )
        try:
            reply = model.chat(
                messages,
                toolbox.schemas,
                max_tokens=settings.model.max_output_tokens if settings.model else None,
                tool_choice="none" if last_call else "auto",
            )
        except ModelError as exc:
            last_call_at = clock()
            failures += 1
            trace.event("model_error", kind=exc.kind, status=exc.status, detail=exc.detail[:300])
            if exc.kind in {"quota", "payment", "auth", "policy", "bad_request", "protocol"}:
                return f"model_{exc.kind}", ""
            if failures >= settings.budget.max_consecutive_model_failures:
                return "model_failures", ""
            sleep(exc.retry_after if exc.retry_after else min(30.0, 5.0 * failures))
            continue
        last_call_at = clock()
        failures = 0
        budget.add_usage(reply.usage.total_tokens, reply.usage.cost_usd)
        if reply.served_by:
            served[reply.served_by] += 1
        trace.event(
            "model_reply",
            served_by=reply.served_by,
            finish_reason=reply.finish_reason,
            usage=reply.usage.__dict__,
            latency_seconds=round(reply.latency_seconds, 3),
            content=reply.content,
            tool_calls=[
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in reply.tool_calls
            ],
        )
        messages.append(reply.message)
        if not reply.tool_calls:
            text = (reply.content or "").strip()
            return ("final_report" if text else "empty_answer"), text
        for call in reply.tool_calls:
            if last_call:
                content = json.dumps(
                    {
                        "refused": "last_model_call",
                        "message": "no more tool calls; write the final report",
                    }
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                continue
            outcome = toolbox.dispatch(call.name, call.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome.content})
            trace.event(
                "tool_result", tool=call.name, status=outcome.status, content=outcome.content[:2000]
            )
            streak = streak + 1 if outcome.signature == last_signature else 1
            last_signature = outcome.signature
        if last_call:
            return "max_model_calls", ""
        if streak >= SAME_CALL_LIMIT:
            trace.event("budget_stop", reason="no_progress")
            return "no_progress", ""


def _write_artifacts(
    settings: Settings,
    outcome: RunOutcome,
    scope: TaskScope,
    started: datetime,
    model_id: str,
) -> None:
    cases_file = outcome.run_dir / "cases.json"
    if not cases_file.exists():
        cases_file.write_text("[]", encoding="utf-8")
    manifest = {
        "run_id": outcome.run_id,
        "task_id": outcome.task_id,
        "build": outcome.build,
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "configured_model": model_id,
        "served_by": outcome.served_by,
        "mixed_model": len(outcome.served_by) > 1,
        "target_commit": settings.target.commit,
        "limits": scope.limits,
        "budget_settings": settings.budget.model_dump(),
        "openai_sdk": openai.__version__,
    }
    (outcome.run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    report = {
        "status": outcome.status,
        "stop_reason": outcome.stop_reason,
        "cases_submitted": outcome.cases,
        "budget": outcome.budget,
        "served_by": outcome.served_by,
        "final_report": outcome.final_report,
    }
    (outcome.run_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    cases = json.loads(cases_file.read_text(encoding="utf-8"))
    lines = [
        f"# Run {outcome.run_id}",
        "",
        f"- task: {outcome.task_id} ({scope.title})",
        f"- status: {outcome.status}, stop reason: {outcome.stop_reason}",
        f"- models that answered: {outcome.served_by or 'none'}",
        f"- budget: {outcome.budget}",
        f"- cases submitted: {len(cases)}",
        "",
    ]
    for case in cases:
        lines.append(f"- {case['case_id']} ({case['rule_id']}): {case['title']} - {case['claim']}")
    lines += ["", "## Final report", "", outcome.final_report or "(none)", ""]
    (outcome.run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
