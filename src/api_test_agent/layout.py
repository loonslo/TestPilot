"""Runtime directory layout and guarded deletion.

The host only deletes directories it created itself: a scenario or seed directory directly under
its managed parent that carries the marker file, or the exported copy directly under ``sut/``.
Paths never come from the model.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from api_test_agent.settings import Settings

MARKER = ".api-test-agent-managed"
REFERENCE = "reference"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class SandboxError(RuntimeError):
    """A sandbox precondition or invariant failed."""


def validate_id(value: str, what: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise SandboxError(f"invalid {what} {value!r}: use 1-40 chars of a-z, 0-9 and '-'")
    return value


@dataclass(frozen=True)
class Layout:
    """Where everything generated lives. All of it sits under the git-ignored ``runtime/``."""

    runtime: Path
    commit: str

    @classmethod
    def from_settings(cls, settings: Settings) -> Layout:
        return cls(runtime=settings.runtime_root, commit=settings.target.commit)

    @property
    def sut_root(self) -> Path:
        return self.runtime / "sut"

    @property
    def sut_dir(self) -> Path:
        return self.sut_root / self.commit[:12]

    @property
    def venv_python(self) -> Path:
        return self.sut_dir / ".venv" / "Scripts" / "python.exe"

    @property
    def secrets_file(self) -> Path:
        return self.runtime / "secrets.json"

    @property
    def seeds_root(self) -> Path:
        return self.runtime / "seeds"

    @property
    def scenarios_root(self) -> Path:
        return self.runtime / "scenarios"

    @property
    def builds_root(self) -> Path:
        return self.runtime / "builds"

    def seed_dir(self, seed_id: str) -> Path:
        return self.seeds_root / validate_id(seed_id, "seed id")

    def scenario_dir(self, scenario_id: str) -> Path:
        return self.scenarios_root / validate_id(scenario_id, "scenario id")

    def build_dir(self, build_id: str) -> Path:
        return self.builds_root / validate_id(build_id, "build id")

    def source_dir(self, build: str) -> Path:
        """Source tree of a build: the pristine reference copy, or a patched copy of it."""
        return self.sut_dir if build == REFERENCE else self.build_dir(build)


def is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or os.path.isjunction(path)


def remove_managed_tree(path: Path, parent: Path, *, require_marker: bool) -> None:
    """Delete ``path`` only if it is a real directory directly inside ``parent``."""
    if is_link_or_junction(path):
        raise SandboxError(f"refusing to delete a link: {path}")
    if not path.is_dir():
        raise SandboxError(f"not a directory: {path}")
    resolved = path.resolve(strict=True)
    if resolved.parent != parent.resolve(strict=True):
        raise SandboxError(f"refusing to delete outside {parent}: {resolved}")
    if require_marker and not (resolved / MARKER).is_file():
        raise SandboxError(f"missing {MARKER}, not created by this tool: {resolved}")
    shutil.rmtree(resolved)
