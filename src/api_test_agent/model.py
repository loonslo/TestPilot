"""The model adapter: one OpenAI-compatible chat call, errors sorted into kinds the runner knows.

Nothing here retries, sleeps or counts: the SDK's own retries are off and the runner owns pacing,
budget and retry decisions. Vendor quirks (a router that changes models, a 200 with an error
body) are absorbed here and never leak into the loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx2
import openai

from api_test_agent.settings import ModelSettings

ErrorKind = Literal[
    "rate_limit",  # too many requests per minute: wait and try again
    "quota",  # the day's free requests are used up: stop
    "timeout",
    "network",
    "server",
    "auth",
    "payment",
    "policy",  # the account's data policy excludes every endpoint of this model
    "bad_request",
    "protocol",  # the answer is not a chat completion we can use
    "unknown",
]
_RETRYABLE: frozenset[str] = frozenset({"rate_limit", "timeout", "network", "server"})


class ModelError(RuntimeError):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind: ErrorKind = kind
        self.detail = message
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in _RETRYABLE


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # the raw JSON text the model produced


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class ModelReply:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    usage: Usage
    served_by: str | None  # the model that answered; a router can differ from call to call
    latency_seconds: float
    message: dict[str, Any]  # the assistant message to append to the conversation


def _error_text(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
        if body.get("message"):
            return str(body["message"])[:500]
    return fallback[:500]


def _retry_after(response: httpx2.Response | None) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def classify_status(exc: openai.APIStatusError) -> ModelError:
    status = exc.status_code
    text = _error_text(exc.body, str(exc))
    lowered = text.lower()
    if status == 429:
        daily = "per-day" in lowered or "per day" in lowered or "daily" in lowered
        kind: ErrorKind = "quota" if daily else "rate_limit"
    elif status == 402:
        kind = "payment"
    elif status in (401, 403):
        kind = "auth"
    elif status == 404 and "data policy" in lowered:
        kind = "policy"
    elif status == 408:
        kind = "timeout"
    elif status in (400, 404, 413, 422):
        kind = "bad_request"
    elif status >= 500:
        kind = "server"
    else:
        kind = "unknown"
    return ModelError(kind, text, status=status, retry_after=_retry_after(exc.response))


class ModelClient:
    def __init__(
        self,
        settings: ModelSettings,
        api_key: str,
        *,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self._settings = settings
        self._api_key = api_key
        self._http = http_client or httpx2.Client()
        self._client = openai.OpenAI(
            base_url=settings.base_url,
            api_key=api_key,
            timeout=settings.timeout_seconds,
            max_retries=0,
            http_client=self._http,
        )

    @property
    def model_id(self) -> str:
        return self._settings.model_id

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        tool_choice: Literal["auto", "none"] = "auto",
    ) -> ModelReply:
        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=self._settings.model_id,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens or self._settings.max_output_tokens,
            )
        except openai.APITimeoutError as exc:
            raise ModelError("timeout", str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise ModelError("network", str(exc)) from exc
        except openai.APIStatusError as exc:
            raise classify_status(exc) from exc
        return self._parse(response, time.monotonic() - started)

    @staticmethod
    def _parse(response: Any, latency: float) -> ModelReply:
        choices = getattr(response, "choices", None)
        if not choices:
            error = getattr(response, "error", None)
            detail = _error_text(
                {"error": error} if isinstance(error, dict) else None, "no choices"
            )
            raise ModelError("protocol", f"the answer has no choices ({detail})")
        choice = choices[0]
        message = choice.message
        calls = tuple(
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments or "",
            )
            for call in (message.tool_calls or [])
            if getattr(call, "function", None) is not None
        )
        assistant: dict[str, Any] = {"role": "assistant", "content": message.content}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in calls
            ]
        usage = getattr(response, "usage", None)
        return ModelReply(
            content=message.content,
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                total_tokens=getattr(usage, "total_tokens", None),
                cost_usd=_as_float(getattr(usage, "cost", None)),
            ),
            served_by=getattr(response, "model", None),
            latency_seconds=latency,
            message=assistant,
        )

    def key_status(self) -> dict[str, Any]:
        """Account facts from ``GET /key``: numbers and flags only, never the key or its label."""
        try:
            response = self._http.get(
                f"{self._settings.base_url}/key",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=20.0,
            )
        except httpx2.HTTPError as exc:
            raise ModelError("network", str(exc)) from exc
        if response.status_code != 200:
            raise ModelError(
                "auth" if response.status_code in (401, 403) else "unknown",
                f"GET /key answered HTTP {response.status_code}",
                status=response.status_code,
            )
        data = response.json().get("data", {})
        safe: dict[str, Any] = {}
        for key in ("is_free_tier", "usage", "limit", "limit_remaining", "limit_reset"):
            if key in data and isinstance(data[key], int | float | bool | type(None) | str):
                safe[key] = data[key]
        free = data.get("free_model_daily_requests")
        if isinstance(free, dict):
            safe["free_model_daily_requests"] = {
                k: free[k] for k in ("used", "limit", "remaining") if k in free
            }
        return safe


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except TypeError, ValueError:
        return None
