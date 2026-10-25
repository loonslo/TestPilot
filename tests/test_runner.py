import json
import shutil
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import FakeModel, fake_target, reply, settings_for

from api_test_agent import capability, runner, sandbox
from api_test_agent.budget import Budget, DailyLedger, Limits
from api_test_agent.layout import SandboxError
from api_test_agent.model import ModelError
from api_test_agent.settings import Settings
from api_test_agent.spec import Spec
from api_test_agent.targethttp import TargetHttp
from api_test_agent.tools import TaskScope, ToolBox
from api_test_agent.trace import Trace

ROOT = Path(__file__).resolve().parents[1]
SPEC = Spec.load(ROOT / "benchmarks" / "public")

CASE: dict[str, Any] = {
    "case_id": "c1",
    "title": "a deposit is counted once",
    "rule_id": "LED-04",
    "claim": "income grows by the deposit",
    "steps": [
        {
            "id": "s1",
            "operation_id": "accounts.upsert",
            "caller": "A",
            "body": {"name": "x", "account_type": "BANK", "currency": "CNY"},
            "expect_status": 200,
        }
    ],
}


def search(query: str = "accounts") -> tuple[str, dict[str, Any]]:
    return ("search_spec", {"query": query})


# --- the loop, called directly ------------------------------------------------------------------


class Env:
    def __init__(self, tmp_path: Path, **budget: Any) -> None:
        self.settings = settings_for(tmp_path, **budget)
        scope = TaskScope(
            task_id="dev-99",
            title="t",
            brief="b",
            rules=("LED-01", "LED-04"),
            operations=("accounts.upsert", "accounts.list", "ledger.summary"),
            callers=("A",),
            limits={},
        )
        self.clock = [0.0]
        self.sleeps: list[float] = []
        self.budget = Budget(
            limits=Limits.for_task(self.settings.budget, {}),
            daily=DailyLedger(tmp_path / "usage.json"),
            max_requests_per_day=self.settings.budget.max_requests_per_day,
            clock=lambda: self.clock[0],
        )
        http = TargetHttp("http://t.test", {"A": "tok"}, SPEC, transport=fake_target())
        self.toolbox = ToolBox(scope, SPEC, http, self.budget, tmp_path)
        self.trace = Trace(tmp_path / "trace.jsonl")
        self.messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
        self.served: Counter[str] = Counter()

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock[0] += seconds

    def run(self, model: FakeModel) -> tuple[str, str]:
        return runner._loop(
            self.settings,
            model,
            self.toolbox,
            self.budget,
            self.messages,
            self.served,
            self.trace,
            self.sleep,
            lambda: self.clock[0],
        )


def protocol_is_closed(messages: list[dict[str, Any]]) -> bool:
    """Every tool call of every assistant message is answered by a tool message, in order."""
    pending: list[str] = []
    for message in messages:
        if message["role"] == "assistant":
            assert not pending, "a new assistant message while tool calls were unanswered"
            pending = [c["id"] for c in message.get("tool_calls", [])]
        elif message["role"] == "tool":
            assert message["tool_call_id"] == pending.pop(0)
    return not pending


def test_a_run_ends_with_the_models_final_report(tmp_path: Path) -> None:
    env = Env(tmp_path)
    model = FakeModel(
        [
            reply(calls=[search()]),
            reply(calls=[("get_operation", {"operation_id": "ledger.summary"})]),
            reply(calls=[("submit_case", CASE)]),
            reply("All rules checked; one case submitted."),
        ]
    )
    reason, report = env.run(model)
    assert (reason, report) == ("final_report", "All rules checked; one case submitted.")
    assert env.budget.model_calls == 4 and len(env.toolbox.cases) == 1
    assert protocol_is_closed(env.messages)
    assert env.served == {"fake/model:free": 4}


def test_the_last_model_call_is_reserved_for_the_report(tmp_path: Path) -> None:
    env = Env(tmp_path, max_model_calls=2)
    model = FakeModel([reply(calls=[search()]), reply("report")])
    reason, report = env.run(model)
    assert (reason, report) == ("final_report", "report")
    first, last = model.requests
    assert first["tool_choice"] == "auto"
    assert last["tool_choice"] == "none"
    assert "last model call" in last["messages"][-1]["content"]


def test_a_model_that_ignores_the_last_call_notice_still_gets_a_closed_protocol(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, max_model_calls=2)
    model = FakeModel([reply(calls=[search("a")]), reply(calls=[search("b")])])
    reason, _ = env.run(model)
    assert reason == "max_model_calls"
    assert protocol_is_closed(env.messages)
    refused = [m for m in env.messages if m["role"] == "tool"][-1]
    assert "last_model_call" in refused["content"]


def test_the_model_call_budget_stops_a_model_that_never_finishes(tmp_path: Path) -> None:
    env = Env(tmp_path, max_model_calls=3)
    model = FakeModel([reply(calls=[search(str(i))]) for i in range(3)])
    assert env.run(model)[0] == "max_model_calls"
    assert env.budget.model_calls == 3


def test_the_same_call_three_times_in_a_row_is_no_progress(tmp_path: Path) -> None:
    env = Env(tmp_path)
    model = FakeModel([reply(calls=[search("same")]) for _ in range(3)])
    assert env.run(model)[0] == "no_progress"
    assert protocol_is_closed(env.messages)


def test_a_different_call_in_between_resets_the_streak(tmp_path: Path) -> None:
    env = Env(tmp_path)
    script = [
        reply(calls=[search("a")]),
        reply(calls=[search("a")]),
        reply(calls=[search("b")]),
        reply(calls=[search("a")]),
        reply("done"),
    ]
    assert env.run(FakeModel(script))[0] == "final_report"


def test_several_tool_calls_in_one_answer_are_all_answered_in_order(tmp_path: Path) -> None:
    env = Env(tmp_path)
    model = FakeModel([reply(calls=[search("a"), search("b"), search("c")]), reply("ok")])
    env.run(model)
    assert protocol_is_closed(env.messages)
    assert len([m for m in env.messages if m["role"] == "tool"]) == 3
    assert env.toolbox.calls_made == 3


def test_broken_tool_arguments_do_not_stop_the_run(tmp_path: Path) -> None:
    env = Env(tmp_path)
    model = FakeModel([reply(calls=[("search_spec", "{broken")]), reply("ok")])
    assert env.run(model)[0] == "final_report"
    tool_message = next(m for m in env.messages if m["role"] == "tool")
    assert "not valid JSON" in tool_message["content"]


def test_an_empty_final_answer_is_not_a_report(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert env.run(FakeModel([reply("   ")])) == ("empty_answer", "")


# --- model failures -------------------------------------------------------------------------


def test_a_rate_limit_waits_and_the_retry_counts_as_a_call(tmp_path: Path) -> None:
    env = Env(tmp_path)
    model = FakeModel(
        [ModelError("rate_limit", "slow down", status=429, retry_after=7), reply("ok")]
    )
    assert env.run(model)[0] == "final_report"
    assert env.sleeps == [7]
    assert env.budget.model_calls == 2 and env.budget.daily.used() == 2


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("quota", "model_quota"),
        ("payment", "model_payment"),
        ("auth", "model_auth"),
        ("policy", "model_policy"),
        ("bad_request", "model_bad_request"),
        ("protocol", "model_protocol"),
    ],
)
def test_errors_that_a_retry_cannot_fix_end_the_run_at_once(
    tmp_path: Path, kind: str, reason: str
) -> None:
    env = Env(tmp_path)
    model = FakeModel([ModelError(kind, "nope", status=400)])  # type: ignore[arg-type]
    assert env.run(model)[0] == reason
    assert env.budget.model_calls == 1


def test_repeated_transient_failures_end_the_run(tmp_path: Path) -> None:
    env = Env(tmp_path)
    model = FakeModel([ModelError("server", "boom") for _ in range(3)])
    assert env.run(model)[0] == "model_failures"
    assert len(env.sleeps) == 2 and env.sleeps[1] > env.sleeps[0]


def test_a_success_clears_the_failure_count(tmp_path: Path) -> None:
    env = Env(tmp_path)
    script = [
        ModelError("server", "boom"),
        ModelError("server", "boom"),
        reply(calls=[search("a")]),
        ModelError("server", "boom"),
        ModelError("server", "boom"),
        reply("ok"),
    ]
    assert env.run(FakeModel(script))[0] == "final_report"


# --- pacing and limits --------------------------------------------------------------------------


def test_calls_are_spaced_by_the_configured_gap(tmp_path: Path) -> None:
    env = Env(tmp_path, min_seconds_between_model_calls=5.0)
    model = FakeModel([reply(calls=[search("a")]), reply(calls=[search("b")]), reply("ok")])
    env.run(model)
    assert env.sleeps == [5.0, 5.0]


def test_the_daily_cap_stops_a_run_before_it_calls_the_model(tmp_path: Path) -> None:
    env = Env(tmp_path, max_requests_per_day=2)
    env.budget.daily.record(2)
    model = FakeModel([])
    assert env.run(model) == ("daily_requests", "")
    assert model.requests == []


def test_the_time_limit_stops_the_run(tmp_path: Path) -> None:
    env = Env(tmp_path, max_seconds=10.0)
    env.clock[0] = 11.0
    assert env.run(FakeModel([]))[0] == "max_seconds"


def test_a_provider_that_reports_a_cost_stops_a_free_run(tmp_path: Path) -> None:
    env = Env(tmp_path)
    paid = reply(calls=[search("a")])
    paid = type(paid)(
        **{**paid.__dict__, "usage": type(paid.usage)(total_tokens=10, cost_usd=0.01)}
    )
    assert env.run(FakeModel([paid, reply("never reached")]))[0] == "max_cost"


# --- run_task with a fake sandbox ----------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    shutil.copytree(ROOT / "benchmarks" / "public", tmp_path / "benchmarks" / "public")
    (tmp_path / "benchmarks" / "dev").mkdir(parents=True)
    shutil.copy(ROOT / "benchmarks" / "dev" / "manifest.json", tmp_path / "benchmarks" / "dev")
    started: list[tuple[str, str]] = []
    stopped: list[str] = []

    def fake_start(
        settings: Settings, scenario_id: str, seed_id: str = "", build: str = "reference"
    ):
        started.append((scenario_id, build))
        return SimpleNamespace(base_url="http://127.0.0.1:9", tokens={"A": "ta", "B": "tb"})

    monkeypatch.setattr(sandbox, "start_scenario", fake_start)
    monkeypatch.setattr(
        sandbox, "stop_scenario", lambda settings, scenario: stopped.append("stopped")
    )
    return SimpleNamespace(
        settings=settings_for(tmp_path), started=started, stopped=stopped, root=tmp_path
    )


def good_script() -> list[Any]:
    return [
        reply(calls=[search()]),
        reply(calls=[("get_operation", {"operation_id": "ledger.summary"})]),
        reply(calls=[("submit_case", CASE)]),
        reply("Checked LED-01 and LED-04; one case submitted."),
    ]


def test_a_completed_run_leaves_every_artifact(project: SimpleNamespace) -> None:
    outcome = runner.run_task(
        project.settings,
        "dev-01",
        model=FakeModel(good_script()),
        run_id="t1",
        sleep=lambda s: None,
    )
    assert outcome.status == "completed" and outcome.stop_reason == "final_report"
    assert outcome.cases == 1 and project.stopped == ["stopped"]
    run = project.root / "artifacts" / "runs" / "t1"
    assert {p.name for p in run.iterdir()} >= {
        "manifest.json", "report.json", "report.md", "trace.jsonl", "cases.json", "evidence",
    }  # fmt: skip
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["build"] == "reference" and manifest["mixed_model"] is False
    assert manifest["configured_model"] == "fake/model" and manifest["served_by"] == {
        "fake/model:free": 4
    }
    events = [
        json.loads(line)["type"] for line in (run / "trace.jsonl").read_text("utf-8").splitlines()
    ]
    assert events[0] == "run_start" and events[-1] == "run_end"
    assert events.count("model_reply") == 4 and events.count("tool_result") == 3
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "one case submitted" in report and "c1 (LED-04)" in report


def test_the_build_comes_from_the_manifest_unless_overridden(project: SimpleNamespace) -> None:
    runner.run_task(project.settings, "dev-02", model=FakeModel([reply("done")]), run_id="a")
    runner.run_task(
        project.settings, "dev-02", model=FakeModel([reply("done")]), run_id="b", build="reference"
    )
    assert [build for _, build in project.started] == ["def-01", "reference"]


def test_models_that_answered_are_recorded_and_a_mixture_is_flagged(
    project: SimpleNamespace,
) -> None:
    script = [
        reply(calls=[search()], served_by="model/one:free"),
        reply("done", served_by="model/two:free"),
    ]
    outcome = runner.run_task(project.settings, "dev-01", model=FakeModel(script), run_id="mix")
    manifest = json.loads((outcome.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mixed_model"] is True
    assert manifest["served_by"] == {"model/one:free": 1, "model/two:free": 1}


def test_a_report_after_hardly_any_work_is_partial_not_completed(project: SimpleNamespace) -> None:
    outcome = runner.run_task(
        project.settings, "dev-01", model=FakeModel([reply("nothing to do")]), run_id="p"
    )
    assert outcome.status == "partial" and outcome.stop_reason == "final_report"


def test_stopping_on_a_limit_is_partial_and_a_dead_model_is_aborted(
    project: SimpleNamespace,
) -> None:
    script = [reply(calls=[search(str(i))]) for i in range(12)]
    outcome = runner.run_task(project.settings, "dev-01", model=FakeModel(script), run_id="lim")
    assert outcome.status == "partial" and outcome.stop_reason == "max_model_calls"
    dead = runner.run_task(
        project.settings, "dev-01", model=FakeModel([ModelError("quota", "daily")]), run_id="dead"
    )
    assert dead.status == "aborted" and dead.stop_reason == "model_quota"


def test_a_bug_in_the_harness_is_not_reported_as_a_model_result(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(messages: list) -> Any:
        raise RuntimeError("harness bug")

    outcome = runner.run_task(project.settings, "dev-01", model=FakeModel([explode]), run_id="bug")
    assert outcome.status == "aborted" and outcome.stop_reason == "harness_error"
    assert project.stopped == ["stopped"]  # the scenario is stopped whatever happens
    assert "harness bug" in capsys.readouterr().err


def test_a_run_id_cannot_be_reused_or_be_unsafe(project: SimpleNamespace) -> None:
    runner.run_task(project.settings, "dev-01", model=FakeModel([reply("x")]), run_id="same")
    with pytest.raises(SandboxError, match="already exists"):
        runner.run_task(project.settings, "dev-01", model=FakeModel([]), run_id="same")
    with pytest.raises(SandboxError):
        runner.run_task(project.settings, "dev-01", model=FakeModel([]), run_id="../escape")


def test_an_unknown_task_is_refused(project: SimpleNamespace) -> None:
    with pytest.raises(FileNotFoundError):
        runner.run_task(project.settings, "dev-77", model=FakeModel([]), run_id="x")


# --- the tool protocol check ---------------------------------------------------------------------


def echo_script(*, args: str = '{"text": "ping"}', name: str = "echo") -> list[Any]:
    return [reply(calls=[(name, args)]), reply("It returned ping.")]


def test_a_model_that_can_use_tools_passes_the_check(tmp_path: Path) -> None:
    ledger = DailyLedger(tmp_path / "u.json")
    report = capability.check_tool_protocol(FakeModel(echo_script()), ledger, sleep=lambda s: None)
    assert report["ok"] is True and report["arguments_valid"] is True
    assert report["turn1"]["served_by"] == "fake/model:free" and ledger.used() == 2


@pytest.mark.parametrize(
    ("script", "problem"),
    [
        ([reply("I will not use tools")], "did not call the echo tool"),
        (echo_script(name="other"), "did not call the echo tool"),
        (echo_script(args="{bad json"), "not valid JSON"),
        (echo_script(args='{"nope": 1}'), "not valid JSON"),
        (
            [reply(calls=[("echo", {"text": "ping"})]), reply(calls=[("echo", {"text": "again"})])],
            "no plain text",
        ),
    ],
)
def test_the_check_says_what_went_wrong(tmp_path: Path, script: list[Any], problem: str) -> None:
    report = capability.check_tool_protocol(
        FakeModel(script), DailyLedger(tmp_path / "u.json"), sleep=lambda s: None
    )
    assert report["ok"] is False and problem in report["problem"]


def test_the_check_reports_a_model_error_with_its_kind(tmp_path: Path) -> None:
    model = FakeModel([ModelError("quota", "daily limit", status=429)])
    report = capability.check_tool_protocol(
        model, DailyLedger(tmp_path / "u.json"), sleep=lambda s: None
    )
    assert report["ok"] is False and report["error"]["kind"] == "quota"
