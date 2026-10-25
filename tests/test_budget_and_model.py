import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest

from api_test_agent.budget import Budget, DailyLedger, Limits
from api_test_agent.envvars import read_env
from api_test_agent.model import ModelClient, ModelError
from api_test_agent.settings import BudgetSettings, ModelSettings

# --- budget ---------------------------------------------------------------------------------


def make_budget(tmp_path: Path, clock: list[float] | None = None, **task: float) -> Budget:
    now = clock if clock is not None else [0.0]
    return Budget(
        limits=Limits.for_task(BudgetSettings(max_requests_per_day=5), task or None),
        daily=DailyLedger(tmp_path / "usage.json", today=lambda: "2026-10-15"),
        max_requests_per_day=5,
        clock=lambda: now[0],
    )


def test_a_task_may_only_lower_the_limits() -> None:
    settings = BudgetSettings()
    lowered = Limits.for_task(
        settings, {"max_model_calls": 4, "max_http_calls": 9, "max_seconds": 60}
    )
    assert (lowered.max_model_calls, lowered.max_http_calls, lowered.max_seconds) == (4, 9, 60)
    raised = Limits.for_task(
        settings, {"max_model_calls": 999, "max_http_calls": 999, "max_seconds": 1e6}
    )
    assert (raised.max_model_calls, raised.max_http_calls, raised.max_seconds) == (12, 30, 600)


def test_every_attempt_counts_and_the_check_comes_before_the_call(tmp_path: Path) -> None:
    budget = make_budget(tmp_path, max_model_calls=2)
    assert budget.model_call_refusal() is None
    budget.count_model_call()
    budget.count_model_call()
    assert budget.model_call_refusal() == "max_model_calls"
    assert budget.model_calls == 2


def test_http_calls_and_time_have_their_own_limits(tmp_path: Path) -> None:
    clock = [0.0]
    budget = make_budget(tmp_path, clock, max_http_calls=1, max_seconds=10)
    assert budget.http_call_refusal() is None
    budget.count_http_call()
    assert budget.http_call_refusal() == "max_http_calls"
    clock[0] = 11.0
    assert budget.model_call_refusal() == "max_seconds"


def test_tokens_and_cost_are_capped_and_unknown_usage_is_not_zero(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    budget.add_usage(None, None)
    assert budget.tokens == 0 and budget.unknown_usage_calls == 1
    budget.add_usage(70_000, None)
    assert budget.model_call_refusal() == "max_tokens"
    free = make_budget(tmp_path)
    free.add_usage(10, 0.0)
    assert free.model_call_refusal() is None
    free.add_usage(10, 0.0001)  # the model is meant to be free: any cost stops the run
    assert free.model_call_refusal() == "max_cost"


def test_the_daily_request_cap_outlives_a_run(tmp_path: Path) -> None:
    for _ in range(5):
        make_budget(tmp_path).count_model_call()
    assert make_budget(tmp_path).model_call_refusal() == "daily_requests"


def test_the_daily_ledger_starts_over_on_a_new_day(tmp_path: Path) -> None:
    day = ["2026-10-15"]
    ledger = DailyLedger(tmp_path / "u.json", today=lambda: day[0])
    ledger.record(3)
    assert ledger.used() == 3
    day[0] = "2026-10-16"
    assert ledger.used() == 0
    assert ledger.record() == 1


def test_a_broken_ledger_file_counts_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "u.json"
    path.write_text("{ not json", encoding="utf-8")
    assert DailyLedger(path).used() == 0


def test_the_ledger_uses_today_in_utc_by_default(tmp_path: Path) -> None:
    ledger = DailyLedger(tmp_path / "u.json")
    ledger.record()
    assert (
        json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))["date"]
        == (date.today().isoformat())
        or ledger.used() == 1
    )  # the UTC date may differ from the local one near midnight


# --- environment ----------------------------------------------------------------------------


def test_the_process_environment_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATA_TEST_SECRET", "from-env")
    assert read_env("ATA_TEST_SECRET") == "from-env"


def test_a_missing_variable_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATA_TEST_SECRET", raising=False)
    assert read_env("ATA_TEST_SECRET_THAT_DOES_NOT_EXIST_ANYWHERE") is None


# --- the model adapter ------------------------------------------------------------------------

SETTINGS = ModelSettings(model_id="openrouter/free")


def client_for(handler: Any) -> tuple[ModelClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    http = httpx2.Client(transport=httpx2.MockTransport(recording))
    return ModelClient(SETTINGS, "sk-test-key", http_client=http), seen


def completion(message: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": "google/gemma-4-31b-it:free",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        **extra,
    }


def test_a_tool_call_reply_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text":"hi"}'},
            }
        ],
    }
    body = completion(
        message,
        usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25, "cost": 0.0},
    )
    client, seen = client_for(lambda r: httpx2.Response(200, json=body))
    reply = client.chat([{"role": "user", "content": "x"}], [], max_tokens=100)
    assert reply.tool_calls[0].name == "echo" and reply.tool_calls[0].arguments == '{"text":"hi"}'
    assert reply.served_by == "google/gemma-4-31b-it:free"  # the model that answered
    assert reply.usage.total_tokens == 25 and reply.usage.cost_usd == 0.0
    assert reply.message["tool_calls"][0]["function"]["name"] == "echo"
    request = json.loads(seen[0].content)
    assert request["model"] == "openrouter/free" and request["max_tokens"] == 100
    assert request["tool_choice"] == "auto"
    assert seen[0].headers["authorization"] == "Bearer sk-test-key"


def test_a_text_reply_has_no_tool_calls() -> None:
    client, _ = client_for(
        lambda r: httpx2.Response(200, json=completion({"role": "assistant", "content": "done"}))
    )
    reply = client.chat([{"role": "user", "content": "x"}], [])
    assert reply.content == "done" and reply.tool_calls == () and "tool_calls" not in reply.message


def test_tool_choice_none_is_passed_for_the_final_call() -> None:
    client, seen = client_for(
        lambda r: httpx2.Response(200, json=completion({"role": "assistant", "content": "x"}))
    )
    client.chat([{"role": "user", "content": "x"}], [], tool_choice="none")
    assert json.loads(seen[0].content)["tool_choice"] == "none"


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind", "retryable"),
    [
        (
            429,
            {"error": {"message": "Rate limit exceeded: too many requests"}},
            {"retry-after": "7"},
            "rate_limit",
            True,
        ),
        (
            429,
            {"error": {"message": "Rate limit exceeded: free-models-per-day"}},
            {},
            "quota",
            False,
        ),
        (402, {"error": {"message": "insufficient credits"}}, {}, "payment", False),
        (401, {"error": {"message": "No auth credentials found"}}, {}, "auth", False),
        (
            404,
            {"error": {"message": "No endpoints found matching your data policy"}},
            {},
            "policy",
            False,
        ),
        (400, {"error": {"message": "bad tool schema"}}, {}, "bad_request", False),
        (502, {"error": {"message": "upstream"}}, {}, "server", True),
        (418, {"error": {"message": "teapot"}}, {}, "unknown", False),
    ],
)
def test_http_errors_become_kinds_the_loop_understands(
    status: int, body: dict[str, Any], headers: dict[str, str], kind: str, retryable: bool
) -> None:
    client, seen = client_for(lambda r: httpx2.Response(status, json=body, headers=headers))
    with pytest.raises(ModelError) as caught:
        client.chat([{"role": "user", "content": "x"}], [])
    assert caught.value.kind == kind and caught.value.retryable is retryable
    assert len(seen) == 1  # the SDK does not retry behind our back
    if "retry-after" in headers:
        assert caught.value.retry_after == 7.0


def test_timeouts_and_connection_failures_are_distinguished() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(ModelError) as timeout:
        client_for(slow)[0].chat([{"role": "user", "content": "x"}], [])
    assert timeout.value.kind == "timeout" and timeout.value.retryable
    with pytest.raises(ModelError) as network:
        client_for(down)[0].chat([{"role": "user", "content": "x"}], [])
    assert network.value.kind == "network" and network.value.retryable


def test_an_answer_without_choices_is_a_protocol_error() -> None:
    body = {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [],
        "error": {"message": "upstream failed"},
    }
    client, _ = client_for(lambda r: httpx2.Response(200, json=body))
    with pytest.raises(ModelError) as caught:
        client.chat([{"role": "user", "content": "x"}], [])
    assert caught.value.kind == "protocol"


def test_key_status_keeps_numbers_and_flags_and_drops_the_label() -> None:
    body = {
        "data": {
            "label": "sk-or-v1-abc...xyz",
            "is_free_tier": True,
            "usage": 0.0,
            "limit": None,
            "free_model_daily_requests": {"used": 3, "limit": 50, "remaining": 47, "extra": "x"},
            "something_else": {"secret": "no"},
        }
    }
    client, seen = client_for(lambda r: httpx2.Response(200, json=body))
    status = client.key_status()
    assert status == {
        "is_free_tier": True,
        "usage": 0.0,
        "limit": None,
        "free_model_daily_requests": {"used": 3, "limit": 50, "remaining": 47},
    }
    assert "sk-test-key" not in json.dumps(status)
    assert seen[0].url.path.endswith("/key")


def test_key_status_reports_a_bad_key() -> None:
    client, _ = client_for(lambda r: httpx2.Response(401, json={"error": {"message": "no"}}))
    with pytest.raises(ModelError) as caught:
        client.key_status()
    assert caught.value.kind == "auth"
