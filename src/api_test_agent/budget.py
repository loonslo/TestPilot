"""The budget of one run, and the day's request count that outlives a run.

Every attempt counts: a retry is a model call, a refused tool call is still a tool call. Checks
run before the action, so a limit that is reached is never exceeded by the call that was about
to be made.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from api_test_agent.settings import BudgetSettings


@dataclass(frozen=True)
class Limits:
    """The limits in force for one task: the configured caps, lowered by the task file."""

    max_model_calls: int
    max_http_calls: int
    max_seconds: float
    max_tokens: int
    max_cost_usd: float

    @classmethod
    def for_task(cls, budget: BudgetSettings, task_limits: dict[str, float] | None) -> Limits:
        task = task_limits or {}
        return cls(
            max_model_calls=min(budget.max_model_calls, int(task.get("max_model_calls", 10**9))),
            max_http_calls=min(budget.max_http_calls, int(task.get("max_http_calls", 10**9))),
            max_seconds=min(budget.max_seconds, float(task.get("max_seconds", 10**9))),
            max_tokens=budget.max_tokens,
            max_cost_usd=budget.max_cost_usd,
        )


class DailyLedger:
    """Counts model requests per UTC day in a small JSON file, across runs and restarts."""

    def __init__(self, path: Path, today: Callable[[], str] | None = None) -> None:
        self._path = path
        self._today = today or (lambda: datetime.now(UTC).strftime("%Y-%m-%d"))

    def used(self) -> int:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return 0
        return int(data.get("requests", 0)) if data.get("date") == self._today() else 0

    def record(self, count: int = 1) -> int:
        total = self.used() + count
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"date": self._today(), "requests": total}), encoding="utf-8"
        )
        return total


@dataclass
class Budget:
    limits: Limits
    daily: DailyLedger
    max_requests_per_day: int
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    model_calls: int = 0
    http_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    unknown_usage_calls: int = 0

    def __post_init__(self) -> None:
        self.started = self.clock()

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    def model_call_refusal(self) -> str | None:
        """Why the next model call may not be made, or None."""
        if self.model_calls >= self.limits.max_model_calls:
            return "max_model_calls"
        if self.elapsed >= self.limits.max_seconds:
            return "max_seconds"
        if self.tokens >= self.limits.max_tokens:
            return "max_tokens"
        if self.cost_usd > self.limits.max_cost_usd:
            return "max_cost"
        if self.daily.used() >= self.max_requests_per_day:
            return "daily_requests"
        return None

    def http_call_refusal(self) -> str | None:
        if self.http_calls >= self.limits.max_http_calls:
            return "max_http_calls"
        if self.elapsed >= self.limits.max_seconds:
            return "max_seconds"
        return None

    def count_model_call(self) -> None:
        """Count an attempt before it is made, so a crash cannot hide it."""
        self.model_calls += 1
        self.daily.record()

    def add_usage(self, total_tokens: int | None, cost_usd: float | None) -> None:
        if total_tokens is None:
            self.unknown_usage_calls += 1
        else:
            self.tokens += total_tokens
        if cost_usd is not None:
            self.cost_usd += cost_usd

    def count_http_call(self) -> None:
        self.http_calls += 1

    def snapshot(self) -> dict[str, float | int]:
        return {
            "model_calls": self.model_calls,
            "http_calls": self.http_calls,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "unknown_usage_calls": self.unknown_usage_calls,
            "elapsed_seconds": round(self.elapsed, 3),
            "requests_today": self.daily.used(),
        }
