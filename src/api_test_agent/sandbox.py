"""Isolated target sandbox: export a pinned commit, run it on a scenario database, reset it.

Everything here is host-side code. The Agent never sees paths, secrets, tokens or logs.
Windows only for now (virtualenv layout, process-tree termination).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets as secrets_lib
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from api_test_agent.layout import MARKER, REFERENCE, Layout, SandboxError, remove_managed_tree
from api_test_agent.settings import TARGET_HOST, Settings

ACCOUNT_ALIASES = ("A", "B")
DEFAULT_SEED = "seed-v1"
# The only variables the target process inherits from this machine. Everything else (API keys,
# proxies, database paths) is dropped so the real environment cannot leak into the sandbox.
_SYSTEM_ENV = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")


def _require_windows() -> None:
    if os.name != "nt":
        raise SandboxError("the sandbox currently supports Windows only")


# --------------------------------------------------------------------------- Git export


def git_read(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise SandboxError(f"git {args[0]} failed: {detail}")
    return proc.stdout


def export_target(settings: Settings, *, force: bool = False) -> Path:
    """Copy the committed files of the pinned commit into ``runtime/sut/<commit>``.

    ``git archive`` only reads the source repository and only emits tracked files, so ``.env``,
    ``data/`` and other untracked local state can never be copied. Git for Windows ships with
    ``core.autocrlf=true``, which makes ``git archive`` rewrite LF to CRLF; the export turns that
    off so the copy is byte-identical to the committed blobs (``verify_copy`` proves it).
    """
    layout = Layout.from_settings(settings)
    repo, commit = settings.target.source_repo, settings.target.commit
    git_read(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    dest = layout.sut_dir
    if dest.exists():
        if not force:
            raise SandboxError(f"{dest} already exists; pass --force to rebuild the copy")
        remove_managed_tree(dest, layout.sut_root, require_marker=False)
    dest.mkdir(parents=True)
    proc = subprocess.Popen(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.autocrlf=false",
            "-C",
            str(repo),
            "archive",
            "--format=tar",
            commit,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            tar.extractall(dest, filter="data")
    finally:
        proc.stdout.close()
    error = proc.stderr.read().decode("utf-8", "replace").strip()
    proc.stderr.close()
    if proc.wait() != 0:
        raise SandboxError(f"git archive failed: {error}")
    return dest


@dataclass(frozen=True)
class CopyReport:
    ok: bool
    expected_files: int
    missing: tuple[str, ...]
    modified: tuple[str, ...]
    extra: tuple[str, ...]
    tree: str


def _blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def verify_copy(settings: Settings) -> CopyReport:
    """Compare the exported copy with the pinned commit, file by file, using Git blob hashes."""
    layout = Layout.from_settings(settings)
    repo, commit = settings.target.source_repo, settings.target.commit
    listing = git_read(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    expected: dict[str, str] = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, kind, sha = meta.decode("ascii").split()
        if kind == "blob" and mode != "120000":
            expected[raw_path.decode("utf-8")] = sha
    missing: list[str] = []
    modified: list[str] = []
    for rel, sha in expected.items():
        file = layout.sut_dir / rel
        if not file.is_file():
            missing.append(rel)
        elif _blob_sha1(file.read_bytes()) != sha:
            modified.append(rel)
    extra: list[str] = []
    if layout.sut_dir.is_dir():
        for current, dirs, files in os.walk(layout.sut_dir):
            if Path(current) == layout.sut_dir and ".venv" in dirs:
                dirs.remove(".venv")
            for name in files:
                rel = (Path(current) / name).relative_to(layout.sut_dir).as_posix()
                if rel not in expected:
                    extra.append(rel)
    tree = git_read(repo, "rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()
    return CopyReport(
        ok=not (missing or modified or extra),
        expected_files=len(expected),
        missing=tuple(sorted(missing)),
        modified=tuple(sorted(modified)),
        extra=tuple(sorted(extra)),
        tree=tree,
    )


def ensure_venv(settings: Settings) -> Path:
    """Create the target's own virtualenv from its lock file; nothing of ours goes into it."""
    layout = Layout.from_settings(settings)
    if not layout.sut_dir.is_dir():
        raise SandboxError("target copy missing; run: api-test-agent sandbox export")
    if layout.venv_python.is_file():
        return layout.venv_python
    command = [
        "uv",
        "sync",
        "--project",
        str(layout.sut_dir),
        "--python",
        settings.target.python,
        "--locked",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode != 0:
        raise SandboxError(f"uv sync failed:\n{proc.stderr.strip()[-2000:]}")
    return layout.venv_python


# --------------------------------------------------------------------------- Secrets and env


@dataclass(frozen=True, repr=False)
class SandboxSecrets:
    """Synthetic, sandbox-only secrets. They live in ``runtime/`` and are never printed."""

    session_secret: str
    password_pepper: str
    email_code_pepper: str
    accounts: Mapping[str, tuple[str, str]]  # alias -> (username, password)

    def __repr__(self) -> str:
        return "SandboxSecrets(<redacted>)"


def load_or_create_secrets(layout: Layout) -> SandboxSecrets:
    path = layout.secrets_file
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = {
            "session_secret": secrets_lib.token_hex(32),
            "password_pepper": secrets_lib.token_hex(32),
            "email_code_pepper": secrets_lib.token_hex(32),
            "accounts": {
                alias: {
                    "username": f"agent_{alias.lower()}",
                    "password": f"Aa1-{secrets_lib.token_urlsafe(18)}",
                }
                for alias in ACCOUNT_ALIASES
            },
        }
        layout.runtime.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return SandboxSecrets(
        session_secret=raw["session_secret"],
        password_pepper=raw["password_pepper"],
        email_code_pepper=raw["email_code_pepper"],
        accounts={a: (v["username"], v["password"]) for a, v in raw["accounts"].items()},
    )


def build_env(
    secrets: SandboxSecrets,
    db_path: Path,
    source: Mapping[str, str] | None = None,
    *,
    pythonpath: Path | None = None,
) -> dict[str, str]:
    """Build the target's environment from scratch (an allow-list, not a filtered copy).

    ``pythonpath`` is only set for patched builds: it makes their source tree win over the
    reference copy that the shared virtualenv points at.
    """
    _require_windows()
    source = os.environ if source is None else source
    env = {key: source[key] for key in _SYSTEM_ENV if key in source}
    system_root = env.get("SYSTEMROOT", r"C:\Windows")
    env["PATH"] = rf"{system_root}\System32;{system_root}"
    env.update(
        {
            "APP_ENV": "development",
            "EMAIL_AUTH_ENABLED": "false",
            "OCR_ENABLED": "false",
            "EXPOSE_VERIFICATION_CODE": "false",
            "DATABASE_PATH": str(db_path),
            "SESSION_SECRET": secrets.session_secret,
            "PASSWORD_PEPPER": secrets.password_pepper,
            "EMAIL_CODE_PEPPER": secrets.email_code_pepper,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
        }
    )
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return env


# --------------------------------------------------------------------------- Target process


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((TARGET_HOST, port)) != 0


class TargetServer:
    """One uvicorn process serving the exported target on one scenario database."""

    def __init__(
        self,
        settings: Settings,
        secrets: SandboxSecrets,
        directory: Path,
        build: str = REFERENCE,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._dir = directory
        self._build = build
        self._layout = Layout.from_settings(settings)
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{TARGET_HOST}:{self._settings.target.port}"

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def start(self) -> None:
        _require_windows()
        if self._proc is not None:
            raise SandboxError("server already started")
        if not self._layout.venv_python.is_file():
            raise SandboxError("target virtualenv missing; run: api-test-agent sandbox venv")
        source_dir = self._layout.source_dir(self._build)
        if not source_dir.is_dir():
            raise SandboxError(
                f"build {self._build!r} not found; run: api-test-agent benchmark build"
            )
        port = self._settings.target.port
        if not _port_is_free(port):
            raise SandboxError(f"port {port} is already in use (orphaned sandbox server?)")
        command = [
            str(self._layout.venv_python),
            "-m",
            "uvicorn",
            self._settings.target.app_module,
            "--host",
            TARGET_HOST,
            "--port",
            str(port),
        ]
        env = build_env(
            self._secrets,
            self._dir / "finunity.db",
            pythonpath=None if self._build == REFERENCE else source_dir / "src",
        )
        with (self._dir / "server.log").open("ab") as log:
            self._proc = subprocess.Popen(
                command,
                cwd=source_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        try:
            self._wait_ready()
        except BaseException:
            self.stop()
            raise

    def _log_tail(self, lines: int = 20) -> str:
        log = self._dir / "server.log"
        if not log.is_file():
            return "(no server log)"
        text = log.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def _wait_ready(self) -> None:
        timeout = self._settings.sandbox.ready_timeout_seconds
        url = self.base_url + self._settings.target.ready_path
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                raise SandboxError("target exited during startup:\n" + self._log_tail())
            try:
                if httpx.get(url, timeout=2.0, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise SandboxError(f"target not ready within {timeout:.0f}s:\n" + self._log_tail())

    def stop(self) -> None:
        """Kill the whole process tree (the venv launcher spawns the real interpreter)."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False
            )
            try:
                proc.wait(timeout=self._settings.sandbox.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
        deadline = time.monotonic() + 10
        while not _port_is_free(self._settings.target.port):
            if time.monotonic() > deadline:
                raise SandboxError("port still in use after stop; an orphaned server may remain")
            time.sleep(0.2)


# --------------------------------------------------------------------------- HTTP helpers


def target_client(base_url: str, token: str | None = None) -> httpx.Client:
    """HTTP client for host-side calls: no proxy from the environment, no redirects."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=15.0,
        follow_redirects=False,
        trust_env=False,
    )


def _error_code(resp: httpx.Response) -> str:
    try:
        return str(resp.json()["error"]["code"])
    except ValueError, KeyError, TypeError:
        return "?"


def _expect_ok(resp: httpx.Response, what: str) -> dict[str, Any]:
    if resp.status_code != 200:
        raise SandboxError(f"{what} failed: HTTP {resp.status_code} {_error_code(resp)}")
    body = resp.json()
    if not isinstance(body, dict):
        raise SandboxError(f"{what} returned a non-object body")
    return body


def _register(client: httpx.Client, username: str, password: str, display_name: str) -> None:
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "password": password,
            "password_confirmation": password,
            "display_name": display_name,
            "accept_privacy": True,
            "accept_terms": True,
        },
    )
    _expect_ok(resp, "register")


def _login(client: httpx.Client, username: str, password: str) -> str:
    resp = client.post(
        "/api/v1/auth/password/login",
        json={
            "username": username,
            "password": password,
            "client_request_id": f"host-login-{uuid.uuid4().hex[:12]}",
        },
    )
    body = _expect_ok(resp, "login")
    token = body.get("data", {}).get("access_token")
    if not isinstance(token, str) or not token:
        raise SandboxError("login response has no access_token")
    return token


# --------------------------------------------------------------------------- SQLite helpers


def _ro_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def snapshot_database(source: Path, destination: Path) -> None:
    """Consistent copy of a (stopped or idle) SQLite database via the backup API."""
    src = sqlite3.connect(_ro_uri(source), uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names
        }
    finally:
        conn.close()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- Seeds and scenarios


def _new_managed_dir(path: Path) -> None:
    path.mkdir(parents=True)
    (path / MARKER).write_text("created by api-test-agent; safe to delete\n", encoding="utf-8")


def build_seed(settings: Settings, seed_id: str = DEFAULT_SEED, *, force: bool = False) -> dict:
    """Start the target on an empty database, register accounts A and B, snapshot the result."""
    layout = Layout.from_settings(settings)
    secrets = load_or_create_secrets(layout)
    seed_dir = layout.seed_dir(seed_id)
    if seed_dir.exists():
        if not force:
            raise SandboxError(f"seed {seed_id!r} already exists; pass --force to rebuild it")
        remove_managed_tree(seed_dir, layout.seeds_root, require_marker=True)
    work_dir = layout.scenario_dir(f"{seed_id}-build")
    if work_dir.exists():
        remove_managed_tree(work_dir, layout.scenarios_root, require_marker=True)
    _new_managed_dir(work_dir)
    server = TargetServer(settings, secrets, work_dir)
    try:
        server.start()
        with target_client(server.base_url) as client:
            for alias, (username, password) in secrets.accounts.items():
                _register(client, username, password, f"Sandbox {alias}")
    finally:
        server.stop()
    _new_managed_dir(seed_dir)
    snapshot_database(work_dir / "finunity.db", seed_dir / "finunity.db")
    meta = {
        "seed_id": seed_id,
        "commit": settings.target.commit,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": sha256_file(seed_dir / "finunity.db"),
        "table_counts": table_counts(seed_dir / "finunity.db"),
    }
    (seed_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    remove_managed_tree(work_dir, layout.scenarios_root, require_marker=True)
    return meta


@dataclass(repr=False)
class Scenario:
    """A running target on a private copy of a seed, with host-held tokens for A and B."""

    scenario_id: str
    seed_id: str
    directory: Path
    server: TargetServer
    tokens: dict[str, str] = field(default_factory=dict)
    build: str = REFERENCE

    @property
    def base_url(self) -> str:
        return self.server.base_url

    def __repr__(self) -> str:
        return f"Scenario({self.scenario_id!r}, seed={self.seed_id!r}, build={self.build!r})"


def start_scenario(
    settings: Settings, scenario_id: str, seed_id: str = DEFAULT_SEED, build: str = REFERENCE
) -> Scenario:
    layout = Layout.from_settings(settings)
    secrets = load_or_create_secrets(layout)
    seed_db = layout.seed_dir(seed_id) / "finunity.db"
    if not seed_db.is_file():
        raise SandboxError(f"seed {seed_id!r} not found; run: api-test-agent sandbox seed")
    directory = layout.scenario_dir(scenario_id)
    if directory.exists():
        raise SandboxError(f"scenario {scenario_id!r} already exists")
    _new_managed_dir(directory)
    server = TargetServer(settings, secrets, directory, build)
    try:
        shutil.copyfile(seed_db, directory / "finunity.db")
        server.start()
        with target_client(server.base_url) as client:
            tokens = {
                alias: _login(client, username, password)
                for alias, (username, password) in secrets.accounts.items()
            }
    except BaseException:
        server.stop()
        remove_managed_tree(directory, layout.scenarios_root, require_marker=True)
        raise
    return Scenario(scenario_id, seed_id, directory, server, tokens, build)


def stop_scenario(settings: Settings, scenario: Scenario, *, remove: bool = True) -> None:
    scenario.server.stop()
    if remove and scenario.directory.exists():
        layout = Layout.from_settings(settings)
        remove_managed_tree(scenario.directory, layout.scenarios_root, require_marker=True)


def reset_scenario(settings: Settings, scenario: Scenario) -> Scenario:
    """Throw the scenario database away and start again from the seed."""
    stop_scenario(settings, scenario, remove=True)
    return start_scenario(settings, scenario.scenario_id, scenario.seed_id, scenario.build)
