"""A small, side-effect-free check that a model can use tools the way the loop needs.

Two requests: the model must call a harmless ``echo`` tool with valid JSON arguments, then, given
the tool's answer under the right ``tool_call_id``, produce a text answer. Nothing touches the
sandbox. It answers one question: is this endpoint good enough to start a run with?
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from api_test_agent.budget import DailyLedger
from api_test_agent.model import ModelError, ModelReply
from api_test_agent.runner import ChatModel

ECHO_TOOL: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Return the given text unchanged.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }
]


def _summary(reply: ModelReply) -> dict[str, Any]:
    return {
        "served_by": reply.served_by,
        "finish_reason": reply.finish_reason,
        "usage": reply.usage.__dict__,
        "latency_seconds": round(reply.latency_seconds, 2),
        "tool_calls": [c.name for c in reply.tool_calls],
        "content": (reply.content or "")[:200],
    }


def check_tool_protocol(
    model: ChatModel,
    ledger: DailyLedger,
    *,
    pause: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the two-request check and report what happened, including any error kind."""
    report: dict[str, Any] = {"model": model.model_id, "ok": False}
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": "Call the echo tool with the text 'ping'. Then tell me what it returned.",
        }
    ]
    try:
        ledger.record()
        first = model.chat(messages, ECHO_TOOL, max_tokens=512)
    except ModelError as exc:
        report["error"] = {"turn": 1, "kind": exc.kind, "status": exc.status, "detail": exc.detail}
        return report
    report["turn1"] = _summary(first)
    if not first.tool_calls or first.tool_calls[0].name != "echo":
        report["problem"] = "the model did not call the echo tool"
        return report
    call = first.tool_calls[0]
    try:
        arguments = json.loads(call.arguments)
        report["arguments_valid"] = isinstance(arguments, dict) and "text" in arguments
    except ValueError:
        report["arguments_valid"] = False
    if not report["arguments_valid"]:
        report["problem"] = "the tool arguments are not valid JSON with a text field"
        return report
    messages.append(first.message)
    messages.append(
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps({"text": arguments["text"]}),
        }
    )
    sleep(pause)
    try:
        ledger.record()
        second = model.chat(messages, ECHO_TOOL, max_tokens=512)
    except ModelError as exc:
        report["error"] = {"turn": 2, "kind": exc.kind, "status": exc.status, "detail": exc.detail}
        return report
    report["turn2"] = _summary(second)
    report["ok"] = bool(second.content and not second.tool_calls)
    if not report["ok"]:
        report["problem"] = "no plain text answer after the tool result"
    return report
