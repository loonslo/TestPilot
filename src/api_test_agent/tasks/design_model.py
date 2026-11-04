"""One budgeted generation call. No target connection or tool access."""

from __future__ import annotations

import json
import math
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from api_test_agent.budget import Budget, DailyLedger, Limits
from api_test_agent.envvars import read_env
from api_test_agent.model import ModelClient
from api_test_agent.settings import BudgetSettings, ModelSettings

KNOWN_MODEL_PRICING: dict[str, dict[str, float]] = {
    # Rates in USD per 1M tokens (input, output)
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "deepseek/deepseek-chat": {"input": 0.14, "output": 0.28},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
}


class GenerationRefused(ValueError):
    def __init__(self, message: str, metadata: dict[str, Any]):
        super().__init__(message)
        self.metadata = metadata


class DesignModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelSettings
    budget: BudgetSettings = BudgetSettings(max_model_calls=1)


@dataclass(frozen=True)
class GeneratedText:
    content: str
    metadata: dict[str, Any]


def live_generator(
    config_path: Path, *, sleep: Callable[[float], None] = time.sleep
) -> Callable[[str], GeneratedText]:
    config = DesignModelSettings.model_validate(tomllib.loads(config_path.read_text("utf-8")))
    ledger_path = Path.cwd() / "runtime" / "model-usage.json"

    def generate(request: str) -> GeneratedText:
        # Do not silently reset a broken daily ledger to zero before a real call.
        if ledger_path.exists():
            ledger = json.loads(ledger_path.read_text("utf-8"))
            if (
                not isinstance(ledger, dict)
                or type(ledger.get("requests")) is not int
                or ledger["requests"] < 0
                or not isinstance(ledger.get("date"), str)
            ):
                raise ValueError("model daily ledger needs reconciliation")
            try:
                date.fromisoformat(ledger["date"])
            except ValueError:
                raise ValueError("model daily ledger date needs reconciliation") from None
        key = read_env(config.model.api_key_env)
        if not key:
            parsed = urlsplit(config.model.base_url)
            if parsed.hostname in {"127.0.0.1", "localhost"}:
                key = "local-endpoint-dummy-key"
            else:
                raise ValueError(
                    f"{config.model.api_key_env} is not set; "
                    "set it as a process or user environment variable"
                )
        budget = Budget(
            Limits.for_task(config.budget, {"max_model_calls": 1}),
            DailyLedger(ledger_path),
            config.budget.max_requests_per_day,
        )
        reason = budget.model_call_refusal()
        if reason:
            raise ValueError(f"generation refused by budget: {reason}")
        sleep(config.budget.min_seconds_between_model_calls)
        reason = budget.model_call_refusal()
        if reason:
            raise ValueError(f"generation refused by budget: {reason}")
        input_upper_bound = len(request.encode("utf-8")) + 512
        output_limit = min(
            config.model.max_output_tokens, budget.limits.max_tokens - input_upper_bound
        )
        if output_limit <= 0:
            raise ValueError("request cannot fit the configured input/output token cap")

        # Pre-call Paid Model Budget Guard with Cost Reservation
        parsed_url = urlsplit(config.model.base_url)
        is_local_endpoint = parsed_url.hostname in {"127.0.0.1", "localhost", "::1"}
        is_verified_free_router = (
            config.model.base_url.rstrip("/").endswith("openrouter.ai/api/v1")
            and config.model.model_id == "openrouter/free"
        )
        is_declared_free = getattr(config.budget, "is_free", False)
        is_free_tier = is_local_endpoint or is_verified_free_router or is_declared_free

        price_in = getattr(config.budget, "cost_per_million_input_tokens", 0.0)
        price_out = getattr(config.budget, "cost_per_million_output_tokens", 0.0)

        if not is_free_tier:
            if config.budget.max_cost_usd <= 0.0:
                raise ValueError(
                    f"Paid model budget guard: external commercial model '{config.model.model_id}' "
                    "requires an explicit positive budget cap (max_cost_usd > 0.0), but got 0.0"
                )
            if config.budget.max_cost_usd > 0.15:
                raise ValueError(
                    f"Paid model budget guard: max_cost_usd={config.budget.max_cost_usd} "
                    "exceeds single-call limit of 0.15 USD (approx. 1.0 RMB)"
                )

            # Determine pricing
            if price_in == 0.0 and price_out == 0.0:
                known = KNOWN_MODEL_PRICING.get(config.model.model_id.lower())
                if known:
                    price_in = known["input"]
                    price_out = known["output"]
                else:
                    raise ValueError(
                        f"Paid model budget guard: unknown pricing for external commercial model "
                        f"'{config.model.model_id}'. Specify 'cost_per_million_input_tokens' and "
                        "'cost_per_million_output_tokens' in budget configuration to enable "
                        "pre-call cost reservation."
                    )

            # Pre-call Worst-case Cost Reservation
            estimated_prompt_tokens = max(1, len(request.encode("utf-8")) // 3) + 256
            worst_case_output_tokens = output_limit

            worst_case_cost = (estimated_prompt_tokens * price_in / 1_000_000.0) + (
                worst_case_output_tokens * price_out / 1_000_000.0
            )

            if worst_case_cost > config.budget.max_cost_usd:
                raise ValueError(
                    f"Pre-call budget reservation exceeded: worst-case estimated cost "
                    f"${worst_case_cost:.5f} (prompt~{estimated_prompt_tokens} tokens, "
                    f"output_cap={worst_case_output_tokens} tokens) exceeds "
                    f"max_cost_usd=${config.budget.max_cost_usd}"
                )

        client = ModelClient(config.model, key)
        budget.count_model_call()
        reply = client.chat(
            [{"role": "user", "content": request}], [], tool_choice="none", max_tokens=output_limit
        )
        budget.add_usage(reply.usage.total_tokens, reply.usage.cost_usd)
        snapshot = budget.snapshot()
        if not math.isfinite(snapshot["cost_usd"]):
            snapshot["cost_usd"] = "invalid_provider_cost"
        metadata = {
            "model": reply.served_by,
            "configured_model": client.model_id,
            "budget": snapshot,
            "finish_reason": reply.finish_reason,
            "latency_seconds": reply.latency_seconds,
        }
        cost = reply.usage.cost_usd
        if cost is None and not is_free_tier:
            prompt_toks = getattr(reply.usage, "prompt_tokens", None) or (
                reply.usage.total_tokens // 2
            )
            completion_toks = getattr(reply.usage, "completion_tokens", None) or (
                reply.usage.total_tokens - prompt_toks
            )
            cost = (prompt_toks * price_in / 1_000_000.0) + (
                completion_toks * price_out / 1_000_000.0
            )

        if cost is not None:
            if not math.isfinite(cost) or cost < 0:
                raise GenerationRefused(
                    "provider reported invalid cost; drafts not imported", metadata
                )
            if is_free_tier and cost > 0:
                raise GenerationRefused(
                    "provider reported cost but budget specifies free model only; "
                    "drafts not imported",
                    metadata,
                )
            if config.budget.max_cost_usd > 0.0 and cost > config.budget.max_cost_usd:
                raise GenerationRefused(
                    f"provider reported cost ${cost:.5f} exceeded budget "
                    f"${config.budget.max_cost_usd}; drafts not imported",
                    metadata,
                )
        if budget.elapsed > budget.limits.max_seconds or budget.tokens > budget.limits.max_tokens:
            raise GenerationRefused(
                "model response exceeded configured budget; drafts not imported", metadata
            )
        if reply.tool_calls or not reply.content:
            raise GenerationRefused(
                "generator must return JSON drafts, not tool calls or empty text", metadata
            )
        return GeneratedText(reply.content, metadata)

    return generate
