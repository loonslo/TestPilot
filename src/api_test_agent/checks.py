"""Readiness checks (``doctor``) and the A1 sandbox acceptance run (``sandbox check``)."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from api_test_agent import sandbox
from api_test_agent.layout import Layout, SandboxError
from api_test_agent.settings import TARGET_HOST, Settings

_VOLATILE_KEYS = {"request_id", "server_time"}
_LOOPBACK_HOSTS = {"127.0.0.1", "[::1]", "0.0.0.0", "[::]", "*"}
_SNAPSHOT_ENDPOINTS = {
    "me": "/api/v1/me",
    "accounts": "/api/v1/accounts",
    "holdings": "/api/v1/holdings",
    "transactions": "/api/v1/transactions",
    "ledger_summary": "/api/v1/ledger/summary?month=2026-10",
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _tool_version(*command: str) -> str | None:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout else None


def run_doctor(settings: Settings) -> list[Check]:
    layout = Layout.from_settings(settings)
    target = settings.target
    checks: list[Check] = [
        Check("host python >= 3.14", sys.version_info >= (3, 14), sys.version.split()[0]),
    ]
    uv_version = _tool_version("uv", "--version")
    checks.append(Check("uv available", uv_version is not None, uv_version or "not found"))
    git_version = _tool_version("git", "--version")
    checks.append(Check("git available", git_version is not None, git_version or "not found"))
    try:
        sandbox.git_read(target.source_repo, "cat-file", "-e", f"{target.commit}^{{commit}}")
        checks.append(Check("pinned commit exists in source repo", True, target.commit[:12]))
    except SandboxError as exc:
        checks.append(Check("pinned commit exists in source repo", False, str(exc)))
    try:
        layout.runtime.mkdir(parents=True, exist_ok=True)
        probe = layout.runtime / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("runtime directory writable", True, "runtime/"))
    except OSError as exc:
        checks.append(Check("runtime directory writable", False, str(exc)))
    free = sandbox._port_is_free(target.port)
    checks.append(Check(f"port {target.port} free", free, "free" if free else "in use"))
    if layout.sut_dir.is_dir():
        report = sandbox.verify_copy(settings)
        detail = f"{report.expected_files} files, tree {report.tree[:12]}"
        if not report.ok:
            detail = (
                f"missing={len(report.missing)} modified={len(report.modified)} "
                f"extra={len(report.extra)}"
            )
        checks.append(Check("target copy matches pinned commit", report.ok, detail))
    else:
        checks.append(Check("target copy matches pinned commit", False, "not exported yet"))
    venv = layout.venv_python.is_file()
    checks.append(Check("target virtualenv present", venv, "ok" if venv else "not created yet"))
    seed = (layout.seed_dir(sandbox.DEFAULT_SEED) / "finunity.db").is_file()
    checks.append(Check("seed present", seed, sandbox.DEFAULT_SEED if seed else "not built yet"))
    return checks


# --------------------------------------------------------------------------- Sandbox check


def normalize(value: Any) -> Any:
    """Drop fields that legitimately differ between runs (ids of requests, timestamps)."""
    if isinstance(value, dict):
        return {
            key: normalize(item)
            for key, item in sorted(value.items())
            if key not in _VOLATILE_KEYS and not key.endswith("_at")
        }
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def snapshot(scenario: sandbox.Scenario) -> dict[str, Any]:
    """Observable state of both accounts, normalised so two runs can be compared."""
    result: dict[str, Any] = {}
    for alias in sorted(scenario.tokens):
        entries: dict[str, Any] = {}
        with sandbox.target_client(scenario.base_url, scenario.tokens[alias]) as client:
            for name, path in _SNAPSHOT_ENDPOINTS.items():
                resp = client.get(path)
                try:
                    body = resp.json()
                except ValueError:
                    body = {"non_json_prefix": resp.text[:60]}
                entries[name] = {"status": resp.status_code, "body": normalize(body)}
        result[alias] = entries
    return result


def _netstat_rows() -> list[tuple[str, str, str, int]]:
    proc = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, check=False)
    rows: list[tuple[str, str, str, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "TCP" and parts[4].isdigit():
            rows.append((parts[1], parts[2], parts[3], int(parts[4])))
    return rows


def egress_snapshot(server_pid: int | None, port: int) -> dict[str, Any]:
    """One point-in-time look at the target's TCP sockets. It cannot prove nothing ever left."""
    rows = _netstat_rows()
    listener = next(
        (
            pid
            for local, _f, state, pid in rows
            if state == "LISTENING" and local.endswith(f":{port}")
        ),
        None,
    )
    pids = {pid for pid in (server_pid, listener) if pid}
    mine = [(local, foreign, state) for local, foreign, state, pid in rows if pid in pids]
    external = [row for row in mine if row[1].rsplit(":", 1)[0] not in _LOOPBACK_HOSTS]
    exposed = [
        row[0] for row in mine if row[2] == "LISTENING" and not row[0].startswith(f"{TARGET_HOST}:")
    ]
    return {
        "pids": sorted(pids),
        "sockets": len(mine),
        "external_connections": external,
        "listening_beyond_loopback": exposed,
    }


def _repo_state(repo: Any) -> dict[str, str]:
    return {
        "head": sandbox.git_read(repo, "rev-parse", "HEAD").decode().strip(),
        "status": sandbox.git_read(repo, "status", "--porcelain").decode("utf-8", "replace"),
    }


def _target_versions(layout: Layout) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json, sys;"
        "print(json.dumps({'python': sys.version.split()[0], **{n: m.version(n) for n in "
        "('fastapi', 'starlette', 'uvicorn', 'pydantic', 'pydantic-settings')}}))"
    )
    proc = subprocess.run(
        [str(layout.venv_python), "-I", "-c", code], capture_output=True, text=True, check=False
    )
    return json.loads(proc.stdout) if proc.returncode == 0 else {"error": proc.stderr[-200:]}


def run_sandbox_check(settings: Settings) -> dict[str, Any]:
    """A1 acceptance: reset consistency, usable accounts, isolation of the source, egress look."""
    layout = Layout.from_settings(settings)
    repo = settings.target.source_repo
    results: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: Any = "") -> bool:
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    started = datetime.now(UTC)
    repo_before = _repo_state(repo)
    copy_before = sandbox.verify_copy(settings)
    record(
        "exported copy equals pinned commit",
        copy_before.ok,
        {"files": copy_before.expected_files, "tree": copy_before.tree},
    )
    report: dict[str, Any] = {
        "started_utc": started.isoformat(timespec="seconds"),
        "commit": settings.target.commit,
        "host_python": sys.version.split()[0],
        "uv": _tool_version("uv", "--version"),
        "target_runtime": _target_versions(layout) if layout.venv_python.is_file() else None,
        "checks": results,
    }
    if not copy_before.ok:
        report["all_ok"] = False
        return report

    seed_dir = layout.seed_dir(sandbox.DEFAULT_SEED)
    if not seed_dir.exists():
        sandbox.build_seed(settings)
    meta = json.loads((seed_dir / "meta.json").read_text(encoding="utf-8"))
    seed_hash = sandbox.sha256_file(seed_dir / "finunity.db")
    record("seed file matches its recorded hash", seed_hash == meta["sha256"], seed_hash[:16])

    scenario = sandbox.start_scenario(settings, "check-1")
    try:
        initial = snapshot(scenario)
        statuses = {
            alias: {name: item["status"] for name, item in entries.items()}
            for alias, entries in initial.items()
        }
        record(
            "accounts A and B authenticate and read their own empty state",
            all(code == 200 for per in statuses.values() for code in per.values()),
            statuses,
        )
        with sandbox.target_client(scenario.base_url, scenario.tokens["A"]) as client_a:
            created = client_a.post(
                "/api/v1/accounts",
                json={"name": "check-account", "account_type": "BANK", "currency": "CNY"},
            )
        with sandbox.target_client(scenario.base_url, scenario.tokens["B"]) as client_b:
            seen_by_b = client_b.get("/api/v1/accounts")
        record(
            "write by A works and is invisible to B",
            created.status_code == 200
            and seen_by_b.status_code == 200
            and normalize(seen_by_b.json()) == initial["B"]["accounts"]["body"],
            {"create_status": created.status_code, "b_list_status": seen_by_b.status_code},
        )
        record(
            "snapshot detects a write (control: the reset checks below are not vacuous)",
            snapshot(scenario) != initial,
        )
        egress = egress_snapshot(scenario.server.pid, settings.target.port)
        record(
            "target sockets: loopback only (point-in-time snapshot)",
            not egress["external_connections"] and not egress["listening_beyond_loopback"],
            egress,
        )
        scenario = sandbox.reset_scenario(settings, scenario)
        after_first = snapshot(scenario)
        record("first reset restores the initial state", after_first == initial)
        scenario = sandbox.reset_scenario(settings, scenario)
        after_second = snapshot(scenario)
        record(
            "second reset equals the first reset and the initial state",
            after_second == after_first == initial,
        )
    finally:
        sandbox.stop_scenario(settings, scenario)

    seed_hash_after = sandbox.sha256_file(seed_dir / "finunity.db")
    record("seed unchanged after all scenarios", seed_hash_after == seed_hash)
    copy_after = sandbox.verify_copy(settings)
    record(
        "exported copy unchanged after running the target",
        copy_after.ok,
        {"missing": copy_after.missing, "modified": copy_after.modified, "extra": copy_after.extra},
    )
    record("source repository HEAD and status unchanged", _repo_state(repo) == repo_before)
    leftover = (
        [p.name for p in layout.scenarios_root.iterdir()] if layout.scenarios_root.is_dir() else []
    )
    record("no scenario directories left behind", not leftover, leftover)
    free = sandbox._port_is_free(settings.target.port)
    record("target port released", free)

    report["all_ok"] = all(item["ok"] for item in results)
    report["finished_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    artifacts = settings.root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    (artifacts / f"sandbox-check-{stamp}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report
