"""Judging submitted cases: replay each on a fresh service of each build, with no model involved.

A case states what a rule requires. On the reference build it must pass; on a faulty build it
should fail. ``classify`` turns the pair of verdicts into the label the evaluation counts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from api_test_agent import benchmark, sandbox
from api_test_agent.cases import CaseModel, CaseVerdict, replay_case
from api_test_agent.layout import REFERENCE
from api_test_agent.settings import Settings
from api_test_agent.spec import Spec
from api_test_agent.targethttp import TargetHttp

Label = Literal["detects", "no_difference", "fails_on_reference", "error", "passes"]


def classify(reference: CaseVerdict, target: CaseVerdict | None) -> Label:
    """How a case fared. ``detects`` needs the reference to pass and the target build to fail."""
    if reference.verdict == "error" or (target is not None and target.verdict == "error"):
        return "error"
    if reference.verdict == "failed":
        return "fails_on_reference"  # it expects something the correct service does not do
    if target is None:
        return "passes"  # a clean task: nothing was found
    return "detects" if target.verdict == "failed" else "no_difference"


def replay_on(settings: Settings, cases: list[CaseModel], build: str) -> list[CaseVerdict]:
    """Replay every case on its own fresh service of the given build."""
    spec = Spec.load(settings.root / "benchmarks" / "public")
    verdicts = []
    for case in cases:
        scenario = sandbox.start_scenario(settings, "replay-1", build=build)
        http = TargetHttp(scenario.base_url, scenario.tokens, spec)
        try:
            verdicts.append(replay_case(case, http))
        finally:
            http.close()
            sandbox.stop_scenario(settings, scenario)
    return verdicts


def replay_cases(settings: Settings, cases: list[CaseModel], build: str) -> list[dict[str, Any]]:
    """Replay on the reference and on ``build`` (the same build means a clean task)."""
    reference = replay_on(settings, cases, REFERENCE)
    target = None if build == REFERENCE else replay_on(settings, cases, build)
    expected = _expected_rules(settings, build)
    rows = []
    for index, case in enumerate(cases):
        ref_verdict = reference[index]
        tgt_verdict = target[index] if target else None
        label = classify(ref_verdict, tgt_verdict)
        rows.append(
            {
                "case_id": case.case_id,
                "rule_id": case.rule_id,
                "title": case.title,
                "label": label,
                "matches_expected_rule": label == "detects" and case.rule_id in expected,
                "reference": {"verdict": ref_verdict.verdict, "detail": ref_verdict.detail},
                "build": (
                    {"verdict": tgt_verdict.verdict, "detail": tgt_verdict.detail}
                    if tgt_verdict
                    else None
                ),
            }
        )
    return rows


def _expected_rules(settings: Settings, build: str) -> set[str]:
    if build == REFERENCE:
        return set()
    for which in benchmark.SETS:
        for defect in benchmark.load_defects(settings, which):
            if defect.id == build:
                return set(defect.expected_violations)
    return set()


def replay_run(settings: Settings, run_dir: Path) -> dict[str, Any]:
    """Judge the cases of one run and write ``replay.json`` next to them."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cases = [
        CaseModel.model_validate(raw)
        for raw in json.loads((run_dir / "cases.json").read_text(encoding="utf-8"))
    ]
    rows = replay_cases(settings, cases, manifest["build"])
    result = {
        "run_id": manifest["run_id"],
        "task_id": manifest["task_id"],
        "build": manifest["build"],
        "cases": rows,
        "detects": sum(r["label"] == "detects" for r in rows),
        "false_positives": sum(r["label"] == "fails_on_reference" for r in rows),
    }
    (run_dir / "replay.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def verify_witnesses(settings: Settings) -> dict[str, Any]:
    """Each hand-written witness must pass on the reference and fail on the build it targets."""
    raw = json.loads(
        (settings.root / "benchmarks" / "dev" / "witness_cases.json").read_text(encoding="utf-8")
    )["witnesses"]
    defects = [d.id for d in benchmark.load_defects(settings)]
    builds = [REFERENCE, *defects]
    cases = [CaseModel.model_validate(item["case"]) for item in raw]
    verdicts = {build: replay_on(settings, cases, build) for build in builds}
    rows = []
    for index, item in enumerate(raw):
        per_build = {build: verdicts[build][index].verdict for build in builds}
        rows.append(
            {
                "case_id": cases[index].case_id,
                "defect": item["defect"],
                "verdicts": per_build,
                "ok": per_build[REFERENCE] == "passed" and per_build[item["defect"]] == "failed",
                "also_fails_on": sorted(
                    b for b, v in per_build.items() if v == "failed" and b != item["defect"]
                ),
            }
        )
    return {"witnesses": rows, "all_ok": all(row["ok"] for row in rows)}
