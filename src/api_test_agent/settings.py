"""Typed configuration: the sandbox target, the model, and the per-run budget.

Machine-specific values (the path of the target's Git repository) live in ``config.local.toml``,
which is never committed. The model's API key is never part of the configuration: only the name
of the environment variable that holds it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

TARGET_HOST = "127.0.0.1"


class TargetSettings(BaseModel):
    """The system under test: a pinned commit of a local Git repository, run on loopback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_repo: Path
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    python: str = Field(default="3.12", pattern=r"^3\.\d{1,2}$")
    port: int = Field(default=18080, ge=1024, le=65535)
    app_module: str = Field(default="finunity_server.main:app", pattern=r"^[\w.]+:\w+$")
    ready_path: str = Field(default="/health/ready", pattern=r"^/[\w/.-]*$")


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_dir: Path = Path("runtime")
    ready_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    stop_timeout_seconds: float = Field(default=15.0, gt=0, le=60)


class ModelSettings(BaseModel):
    """An OpenAI-compatible chat endpoint. SDK retries are always off; the host retries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(
        default="https://openrouter.ai/api/v1", pattern=r"^https?://[\w.-]+(:\d+)?(/[\w./-]*)?$"
    )
    model_id: str = Field(pattern=r"^[\w.:/-]+$")
    api_key_env: str = Field(default="API_TEST_AGENT_MODEL_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: float = Field(default=90.0, gt=0, le=300)
    max_output_tokens: int = Field(default=2048, gt=0, le=16384)


class BudgetSettings(BaseModel):
    """Caps for one task run. A task file may only lower the call and time limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_model_calls: int = Field(default=12, gt=0, le=100)
    max_http_calls: int = Field(default=30, gt=0, le=500)
    max_seconds: float = Field(default=600.0, gt=0, le=7200)
    max_tokens: int = Field(default=60_000, gt=0)
    # The configured model is free: any cost the provider reports stops the run.
    max_cost_usd: float = Field(default=0.0, ge=0)
    # Free-tier accounts get 50 requests a day (1000 after 10 credits were bought).
    max_requests_per_day: int = Field(default=45, gt=0)
    min_seconds_between_model_calls: float = Field(default=3.0, ge=0, le=60)
    max_consecutive_model_failures: int = Field(default=3, gt=0, le=10)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: TargetSettings
    sandbox: SandboxSettings = SandboxSettings()
    model: ModelSettings | None = None
    budget: BudgetSettings = BudgetSettings()
    root: Path

    @classmethod
    def load(cls, path: Path) -> Settings:
        """Read a TOML file; relative paths resolve against the file's directory."""
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if "root" in data:
            raise ValueError("'root' is derived from the config file location, not configurable")
        return cls.model_validate({**data, "root": path.resolve().parent})

    @property
    def runtime_root(self) -> Path:
        base = self.sandbox.runtime_dir
        return (base if base.is_absolute() else self.root / base).resolve()
