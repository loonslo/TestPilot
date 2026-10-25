"""The development benchmark: build the defect builds, label them with the conformance suite.

``verify_benchmark`` is the acceptance run for task A2: the reference build must satisfy every
rule, and every defect build must violate exactly the rules its definition promises, nothing
more and nothing less. The suite's verdicts are the labels the Agent will later be scored on.

Two sets of builds exist. ``defects`` are the five injected defects behind the Agent's tasks.
``mutants`` are extra single-change builds that only exist to show the suite can fail on rules
the five defects do not touch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api_test_agent import builds, conformance, sandbox
from api_test_agent.layout import REFERENCE, Layout, SandboxError
from api_test_agent.settings import Settings

SETS = ("defects", "mutants")


def dev_dir(settings: Settings) -> Path:
    return settings.root / "benchmarks" / "dev"


def rules_path(settings: Settings) -> Path:
    return settings.root / "benchmarks" / "public" / "rules.json"


def load_defects(settings: Settings, which: str = "defects") -> list[builds.Defect]:
    if which not in SETS:
        raise SandboxError(f"unknown set {which!r}; choose from {SETS}")
    return builds.load_defects(dev_dir(settings) / f"{which}.json")


def build_all(settings: Settings, which: str = "defects", *, force: bool = False) -> list[dict]:
    """Create every build of a set and check each one changed only the files it should."""
    layout = Layout.from_settings(settings)
    secrets = sandbox.load_or_create_secrets(layout)
    report = []
    for defect in load_defects(settings, which):
        if layout.build_dir(defect.id).exists() and not force:
            report.append({"id": defect.id, "status": "exists"})
            continue
        builds.build_defect(settings, defect, force=force)
        changed = builds.changed_files(settings, defect)
        wanted = sorted({replacement.file for replacement in defect.replacements})
        origin = builds.module_origin(settings, secrets, defect.id)
        in_build = layout.build_dir(defect.id).resolve() in origin.resolve().parents
        report.append(
            {
                "id": defect.id,
                "status": "built",
                "changed_files": changed,
                "only_expected_files": changed == wanted,
                "imports_from_build": in_build,
            }
        )
    return report


def run_build(settings: Settings, build: str) -> list[conformance.RuleResult]:
    scenario = sandbox.start_scenario(settings, f"conf-{build}", build=build)
    try:
        return conformance.run_conformance(scenario, conformance.rule_order(rules_path(settings)))
    finally:
        sandbox.stop_scenario(settings, scenario)


def verify_benchmark(
    settings: Settings, which: str = "defects", *, only: list[str] | None = None
) -> dict[str, Any]:
    """Run the conformance suite on the reference and on every build of the chosen set."""
    plan = [(REFERENCE, frozenset())]
    plan += [(d.id, frozenset(d.expected_violations)) for d in load_defects(settings, which)]
    builds_report = []
    for build, expected in plan:
        if only and build not in only:
            continue
        results = run_build(settings, build)
        violated = frozenset(r.rule_id for r in results if r.outcome == "violation")
        errors = [r for r in results if r.outcome == "error"]
        builds_report.append(
            {
                "build": build,
                "expected_violations": sorted(expected),
                "violated": sorted(violated),
                "errors": [{"rule": r.rule_id, "detail": r.detail} for r in errors],
                "ok": violated == expected and not errors,
                "results": [
                    {"rule": r.rule_id, "outcome": r.outcome, "detail": r.detail} for r in results
                ],
            }
        )
    return {
        "set": which,
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": settings.target.commit,
        "builds": builds_report,
        "all_ok": all(item["ok"] for item in builds_report),
    }


def write_report(settings: Settings, name: str, report: dict[str, Any]) -> Path:
    artifacts = settings.root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = artifacts / f"{name}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
