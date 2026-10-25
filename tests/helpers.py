"""Shared test doubles: a scripted model and a tiny in-memory target."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx

from api_test_agent.model import ModelError, ModelReply, ToolCall, Usage
from api_test_agent.settings import BudgetSettings, Settings

PUBLIC = Path(__file__).resolve().parents[1] / "benchmarks" / "public"


def reply(
    content: str | None = None,
    calls: list[tuple[str, dict[str, Any] | str]] | None = None,
    *,
    served_by: str = "fake/model:free",
    tokens: int = 100,
) -> ModelReply:
    """A model answer: text, or tool calls given as (name, arguments-as-dict-or-raw-text)."""
    tool_calls = tuple(
        ToolCall(
            id=f"call_{index}",
            name=name,
            arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
        )
        for index, (name, arguments) in enumerate(calls or [])
    )
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in tool_calls
        ]
    return ModelReply(
        content=content,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=Usage(prompt_tokens=tokens - 10, completion_tokens=10, total_tokens=tokens),
        served_by=served_by,
        latency_seconds=0.01,
        message=message,
    )


class FakeModel:
    """Plays a fixed script of replies (or errors) and records what it was asked."""

    def __init__(self, script: list[ModelReply | ModelError | Callable[[list], ModelReply]]):
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.model_id = "fake/model"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        tool_choice: Literal["auto", "none"] = "auto",
    ) -> ModelReply:
        self.requests.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice}
        )
        if not self._script:
            raise AssertionError("the model was called more often than scripted")
        step = self._script.pop(0)
        if isinstance(step, ModelError):
            raise step
        return step(messages) if callable(step) else step


def settings_for(tmp_path: Path, **budget: Any) -> Settings:
    defaults: dict[str, Any] = {"min_seconds_between_model_calls": 0.0}
    defaults.update(budget)
    return Settings.model_validate(
        {
            "target": {"source_repo": tmp_path, "commit": "a" * 40},
            "budget": BudgetSettings(**defaults),
            "root": tmp_path,
        }
    )


def fake_target(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> httpx.MockTransport:
    """An in-memory service: every path answers 200 with a small envelope unless told otherwise."""

    def default(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") is None:
            error = {"code": "UNAUTHORIZED", "message": "no token"}
            return httpx.Response(401, json={"error": error})
        return httpx.Response(
            200,
            json={
                "request_id": "r",
                "data": {"items": [], "path": request.url.path},
                "warnings": [],
            },
        )

    return httpx.MockTransport(handler or default)
