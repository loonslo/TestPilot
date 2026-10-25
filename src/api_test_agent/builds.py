"""Defect builds: the reference copy plus one small, exact source change each.

A defect is a list of exact-text replacements. Each ``old`` string must match exactly once, so a
change in the reference copy makes a build fail loudly instead of silently producing a different
defect. Builds live next to the reference copy under ``runtime/builds`` and reuse its virtualenv;
the patched source tree wins through ``PYTHONPATH`` (see ``sandbox.build_env``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from api_test_agent import sandbox
from api_test_agent.layout import (
    MARKER,
    REFERENCE,
    Layout,
    SandboxError,
    remove_managed_tree,
    validate_id,
)
from api_test_agent.settings import Settings


class Replacement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str = Field(pattern=r"^[\w./-]+$")
    old: str = Field(min_length=1)
    new: str


class Defect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    rules: tuple[str, ...] = Field(min_length=1)
    expected_violations: tuple[str, ...] = Field(min_length=1)
    replacements: tuple[Replacement, ...] = Field(min_length=1)


def load_defects(path: Path) -> list[Defect]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    defects = [Defect.model_validate(item) for item in raw["defects"]]
    ids = [validate_id(defect.id, "defect id") for defect in defects]
    if len(ids) != len(set(ids)):
        raise SandboxError("duplicate defect ids")
    if REFERENCE in ids:
        raise SandboxError(f"{REFERENCE!r} is reserved for the pristine copy")
    return defects


def apply_replacements(root: Path, defect: Defect) -> None:
    """Apply the defect's replacements to a copy at ``root``, byte-exactly."""
    resolved_root = root.resolve()
    for replacement in defect.replacements:
        path = (root / replacement.file).resolve()
        if resolved_root not in path.parents:
            raise SandboxError(f"{defect.id}: {replacement.file} leaves the build directory")
        if not path.is_file():
            raise SandboxError(f"{defect.id}: {replacement.file} does not exist")
        text = path.read_bytes().decode("utf-8")
        found = text.count(replacement.old)
        if found != 1:
            raise SandboxError(
                f"{defect.id}: expected exactly one match in {replacement.file}, found {found}"
            )
        path.write_bytes(text.replace(replacement.old, replacement.new, 1).encode("utf-8"))


def build_defect(settings: Settings, defect: Defect, *, force: bool = False) -> Path:
    """Copy the reference tree (without its virtualenv) and apply one defect to the copy."""
    layout = Layout.from_settings(settings)
    reference = layout.sut_dir
    if not reference.is_dir():
        raise SandboxError("reference copy missing; run: api-test-agent sandbox export")
    destination = layout.build_dir(defect.id)
    if destination.exists():
        if not force:
            raise SandboxError(f"build {defect.id!r} already exists; pass --force to rebuild it")
        remove_managed_tree(destination, layout.builds_root, require_marker=True)
    layout.builds_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(reference, destination, ignore=shutil.ignore_patterns(".venv"))
    (destination / MARKER).write_text("created by api-test-agent; safe to delete\n", "utf-8")
    try:
        apply_replacements(destination, defect)
    except BaseException:
        remove_managed_tree(destination, layout.builds_root, require_marker=True)
        raise
    return destination


def _tree_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for current, dirs, files in os.walk(root):
        if Path(current) == root and ".venv" in dirs:
            dirs.remove(".venv")
        for name in files:
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if rel != MARKER:
                hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def changed_files(settings: Settings, defect: Defect) -> list[str]:
    """Files whose content differs between the reference copy and the build (sorted)."""
    layout = Layout.from_settings(settings)
    reference = _tree_hashes(layout.sut_dir)
    build = _tree_hashes(layout.build_dir(defect.id))
    return sorted(
        path for path in reference.keys() | build.keys() if reference.get(path) != build.get(path)
    )


def module_origin(settings: Settings, secrets: sandbox.SandboxSecrets, build: str) -> Path:
    """Where ``finunity_server`` is imported from when the target is started as ``build``."""
    layout = Layout.from_settings(settings)
    source_dir = layout.source_dir(build)
    env = sandbox.build_env(
        secrets,
        layout.runtime / "unused.db",
        pythonpath=None if build == REFERENCE else source_dir / "src",
    )
    proc = subprocess.run(
        [str(layout.venv_python), "-c", "import finunity_server; print(finunity_server.__file__)"],
        cwd=source_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SandboxError(f"cannot import the target package: {proc.stderr.strip()[-300:]}")
    return Path(proc.stdout.strip())
