"""Single-call, tool-free model adapter for case design."""

from __future__ import annotations

import math
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from api_test_agent.envvars import read_env
from api_test_agent.model import ModelClient
from api_test_agent.settings import BudgetSettings, ModelSettings


class GenerationRefused(ValueError):
    def __init__(self, message: str, metadata: dict[str, Any]):
        super().__init__(message)
        self.metadata = metadata


class DesignModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelSettings
    # Accept existing CLI files. The token cap is a generation size limit.
    budget: BudgetSettings = BudgetSettings(max_model_calls=1)


@dataclass(frozen=True)
class GeneratedText:
    content: str
    metadata: dict[str, Any]


def live_generator(config_path: Path) -> Callable[[str], GeneratedText]:
    config = DesignModelSettings.model_validate(tomllib.loads(config_path.read_text("utf-8")))
    return model_generator(config.model, max_tokens=config.budget.max_tokens)


def model_generator(
    settings: ModelSettings, *, max_tokens: int | None = None
) -> Callable[[str], GeneratedText]:
    def generate(request: str) -> GeneratedText:
        key = read_env(settings.api_key_env)
        if not key:
            if urlsplit(settings.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError(f"Set the {settings.api_key_env} environment variable first")
            key = "local-endpoint-dummy-key"
        output_limit = settings.max_output_tokens
        if max_tokens is not None:
            output_limit = min(output_limit, max_tokens - len(request.encode("utf-8")) - 512)
        if output_limit <= 0:
            raise ValueError("request cannot fit the configured input/output token cap")

        client = ModelClient(settings, key)
        reply = client.chat(
            [{"role": "user", "content": request}], [], tool_choice="none", max_tokens=output_limit
        )

        # Provider response bodies can echo authentication values. Do not persist them.
        def redact(value: str | None) -> str | None:
            return value.replace(key, "[redacted]") if value is not None else None

        cost = reply.usage.cost_usd
        metadata = {
            "model": redact(reply.served_by),
            "configured_model": redact(client.model_id),
            "finish_reason": redact(reply.finish_reason),
            "latency_seconds": reply.latency_seconds,
            "total_tokens": reply.usage.total_tokens,
            "provider_cost_usd": cost if cost is not None and math.isfinite(cost) else None,
        }
        if reply.tool_calls or not reply.content or reply.finish_reason == "length":
            raise GenerationRefused(
                "model must return complete JSON drafts without tools", metadata
            )
        return GeneratedText(reply.content.replace(key, "[redacted]"), metadata)

    return generate
