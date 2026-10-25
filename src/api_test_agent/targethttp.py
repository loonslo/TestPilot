"""Calls to the target on behalf of the Agent: fixed routes, injected tokens, bounded answers.

The model only ever names an operation, a caller alias and parameters. The route comes from the
public spec, the token from the host, and the address is the loopback port of the sandbox, so the
model cannot reach another URL, another user, or see a token.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from api_test_agent.spec import Spec

CALLERS = ("A", "B", "anonymous", "invalid_token")
INVALID_TOKEN = "invalid-token-for-testing"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MAX_MODEL_BYTES = 4096


class CallError(ValueError):
    """The request is not allowed or malformed; nothing was sent."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any  # parsed JSON, or None when the answer was not JSON
    raw_text: str
    seconds: float

    @property
    def error_code(self) -> str | None:
        error = self.body.get("error") if isinstance(self.body, dict) else None
        return error.get("code") if isinstance(error, dict) else None


class TargetHttp:
    def __init__(
        self,
        base_url: str,
        tokens: dict[str, str],
        spec: Spec,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._spec = spec
        self._tokens = tokens
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _headers(self, caller: str) -> dict[str, str]:
        if caller == "anonymous":
            return {}
        if caller == "invalid_token":
            return {"Authorization": f"Bearer {INVALID_TOKEN}"}
        if caller in self._tokens:
            return {"Authorization": f"Bearer {self._tokens[caller]}"}
        raise CallError(f"unknown caller {caller!r}")

    def call(
        self,
        operation_id: str,
        caller: str,
        path_params: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        body: Any = None,
    ) -> HttpResult:
        return self.send(self.prepare(operation_id, caller, path_params, query, body))

    def prepare(
        self,
        operation_id: str,
        caller: str,
        path_params: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        body: Any = None,
    ) -> httpx.Request:
        """Validate and build a request without sending it; raises ``CallError`` if not allowed."""
        operation = self._spec.operations.get(operation_id)
        if operation is None:
            raise CallError(f"unknown operation {operation_id!r}")
        values = path_params or {}
        missing = [name for name in operation.path_params if name not in values]
        extra = [name for name in values if name not in operation.path_params]
        if missing or extra:
            raise CallError(f"path parameters must be exactly {list(operation.path_params)}")
        path = operation.path
        for name in operation.path_params:
            value = str(values[name])
            if not _SEGMENT_RE.fullmatch(value):
                raise CallError(f"path parameter {name!r} is not a plain identifier")
            path = path.replace("{" + name + "}", urllib.parse.quote(value, safe=""))
        unknown_query = [k for k in (query or {}) if k not in operation.query_params]
        if unknown_query:
            raise CallError(
                f"unknown query parameters {unknown_query}; allowed: {list(operation.query_params)}"
            )
        headers = self._headers(caller)
        return self._client.build_request(
            operation.method,
            path,
            params=query or None,
            json=body if operation.method != "GET" else None,
            headers=headers,
        )

    def send(self, request: httpx.Request) -> HttpResult:
        began = time.monotonic()
        response = self._client.send(request)
        seconds = time.monotonic() - began
        text = response.text
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        return HttpResult(status=response.status_code, body=parsed, raw_text=text, seconds=seconds)


def bound_for_model(result: HttpResult, limit: int = MAX_MODEL_BYTES) -> tuple[Any, bool]:
    """The part of an answer the model gets: the JSON itself when small, a cut text otherwise."""
    text = (
        json.dumps(result.body, ensure_ascii=False) if result.body is not None else result.raw_text
    )
    if len(text.encode("utf-8")) <= limit:
        return (result.body if result.body is not None else text), False
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore"), True
