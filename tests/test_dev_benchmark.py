"""Consistency of the development benchmark files and of the build machinery."""

import json
import re
from pathlib import Path

import pytest

from api_test_agent import conformance
from api_test_agent.builds import Defect, Replacement, apply_replacements, load_defects
from api_test_agent.layout import REFERENCE, Layout, SandboxError
from api_test_agent.sandbox import SandboxSecrets, build_env
from api_test_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "benchmarks" / "dev"
PUBLIC = ROOT / "benchmarks" / "public"
HINTS = [
    r"reference (build|service|implementation|copy)",
    r"pristine",
    r"unmodified",
    r"def-\d",
    r"mut-\d",
    r"defect",
    r"mutant",
    r"inject",
    r"\bpatch\b",
    r"baseline",
]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict:
    return load(DEV / "manifest.json")


@pytest.fixture(scope="module")
def rules() -> list[dict]:
    return load(PUBLIC / "rules.json")["rules"]


@pytest.fixture(scope="module")
def defects() -> list[Defect]:
    return load_defects(DEV / "defects.json")


@pytest.fixture(scope="module")
def mutants() -> list[Defect]:
    return load_defects(DEV / "mutants.json")


def spec_operations() -> set[str]:
    spec = load(PUBLIC / "openapi.json")
    return {op["operationId"] for item in spec["paths"].values() for op in item.values()}


# --- manifest ------------------------------------------------------------------------------


def test_ten_tasks_five_on_defect_builds_five_on_the_pristine_one(manifest: dict) -> None:
    builds = [task["build"] for task in manifest["tasks"]]
    assert len(builds) == len({task["id"] for task in manifest["tasks"]}) == 10
    assert builds.count(REFERENCE) == 5
    assert sorted(b for b in builds if b != REFERENCE) == [f"def-0{n}" for n in range(1, 6)]


def test_every_scope_is_used_once_on_a_defect_and_once_on_the_pristine_build(
    manifest: dict,
) -> None:
    for scope in manifest["scopes"]:
        used = [t["build"] for t in manifest["tasks"] if t["scope"] == scope]
        assert len(used) == 2 and REFERENCE in used, scope


def test_scopes_split_the_rules_without_overlap_or_gaps(manifest: dict, rules: list[dict]) -> None:
    listed = [rule for scope in manifest["scopes"].values() for rule in scope["rules"]]
    assert sorted(listed) == sorted(rule["id"] for rule in rules)


def test_scope_operations_exist_in_the_public_spec(manifest: dict) -> None:
    for name, scope in manifest["scopes"].items():
        assert set(scope["operations"]) <= spec_operations(), name


def test_scope_callers_cover_what_the_rules_need(manifest: dict) -> None:
    for scope in manifest["scopes"].values():
        assert "A" in scope["callers"]
        if any(rule.startswith("ISO") for rule in scope["rules"]):
            assert {"B", "anonymous", "invalid_token"} <= set(scope["callers"])


def test_each_defect_task_gives_the_agent_the_rules_the_defect_breaks(
    manifest: dict, defects: list[Defect]
) -> None:
    by_id = {defect.id: defect for defect in defects}
    for task in manifest["tasks"]:
        if task["build"] == REFERENCE:
            continue
        scope_rules = set(manifest["scopes"][task["scope"]]["rules"])
        assert set(by_id[task["build"]].expected_violations) <= scope_rules, task["id"]


# --- Agent-facing task files -----------------------------------------------------------------


def test_public_tasks_are_exactly_what_the_manifest_describes(manifest: dict) -> None:
    files = {path.stem: load(path) for path in (PUBLIC / "tasks").glob("dev-*.json")}
    assert set(files) == {task["id"] for task in manifest["tasks"]}
    for task in manifest["tasks"]:
        scope = manifest["scopes"][task["scope"]]
        shown = files[task["id"]]
        assert shown["rules"] == scope["rules"]
        assert shown["operations"] == scope["operations"]
        assert shown["callers"] == scope["callers"]
        assert shown["limits"] == {"max_model_calls": 12, "max_http_calls": 30, "max_seconds": 600}


def test_nothing_the_agent_reads_hints_at_the_benchmark_design() -> None:
    files = [PUBLIC / name for name in ("openapi.json", "rules.json", "response-notes.json")]
    files += sorted((PUBLIC / "tasks").glob("dev-*.json"))
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for pattern in HINTS:
            assert not re.search(pattern, text), f"{path.name} matches {pattern}"


def test_task_files_do_not_reveal_which_tasks_have_a_defect(manifest: dict) -> None:
    by_scope: dict[str, list[dict]] = {}
    for task in manifest["tasks"]:
        by_scope.setdefault(task["scope"], []).append(load(PUBLIC / "tasks" / f"{task['id']}.json"))
    for pair in by_scope.values():
        first, second = pair
        assert {k: v for k, v in first.items() if k != "task_id"} == {
            k: v for k, v in second.items() if k != "task_id"
        }


# --- definitions of defects and mutants ---------------------------------------------------------


def test_definitions_are_unique_and_reference_real_rules(
    defects: list[Defect], mutants: list[Defect], rules: list[dict]
) -> None:
    ids = [item.id for item in defects + mutants]
    assert len(ids) == len(set(ids)) == 14  # five defects and nine mutants
    known = {rule["id"] for rule in rules}
    for item in defects + mutants:
        assert set(item.rules) <= set(item.expected_violations) <= known, item.id
        assert all(r.file.startswith("src/finunity_server/") for r in item.replacements), item.id


def test_the_suite_has_a_check_for_every_rule(rules: list[dict]) -> None:
    assert set(conformance.CHECKS) == {rule["id"] for rule in rules}
    assert conformance.rule_order(PUBLIC / "rules.json") == [rule["id"] for rule in rules]


def test_which_rules_the_builds_exercise() -> None:
    """Documents the suite's proven sensitivity: rules without a build are untested as failures."""
    defects = load_defects(DEV / "defects.json")
    mutants = load_defects(DEV / "mutants.json")
    covered = {rule for item in defects + mutants for rule in item.expected_violations}
    assert covered == {
        "LED-01", "LED-02", "LED-03", "LED-04", "LED-05", "LED-06", "LED-07",
        "ARC-01", "ARC-02", "ARC-03", "ARC-04", "ARC-05",
        "ISO-01", "ISO-02", "ISO-03",
    }  # fmt: skip
    # Still without any failing build: LED-08, LED-09 and ISO-04.


# --- patch application ---------------------------------------------------------------------------


def defect_with(*replacements: Replacement) -> Defect:
    return Defect(
        id="def-99", title="t", rules=("X",), expected_violations=("X",), replacements=replacements
    )


def test_a_replacement_must_match_exactly_once(tmp_path: Path) -> None:
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir()
    target.write_bytes(b"x = 1\ny = 1\nz = 2\n")
    once = defect_with(Replacement(file="src/a.py", old="z = 2", new="z = 3"))
    apply_replacements(tmp_path, once)
    assert target.read_bytes() == b"x = 1\ny = 1\nz = 3\n"
    with pytest.raises(SandboxError, match="found 0"):
        apply_replacements(tmp_path, once)  # already applied: the old text is gone
    twice = defect_with(Replacement(file="src/a.py", old="= 1", new="= 9"))
    with pytest.raises(SandboxError, match="found 2"):
        apply_replacements(tmp_path, twice)


def test_replacements_keep_the_bytes_around_them_including_crlf(tmp_path: Path) -> None:
    target = tmp_path / "src" / "b.py"
    target.parent.mkdir()
    target.write_bytes("a = 1\r\n# 中文\r\nb = 2\r\n".encode())
    apply_replacements(
        tmp_path, defect_with(Replacement(file="src/b.py", old="b = 2", new="b = 5"))
    )
    assert target.read_bytes() == "a = 1\r\n# 中文\r\nb = 5\r\n".encode()


def test_a_replacement_cannot_leave_the_build_directory(tmp_path: Path) -> None:
    build = tmp_path / "build"
    (build / "src").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    sneaky = defect_with(Replacement(file="../outside.py", old="secret = 1", new="secret = 2"))
    with pytest.raises(SandboxError, match="leaves the build"):
        apply_replacements(build, sneaky)
    assert outside.read_text(encoding="utf-8") == "secret = 1\n"


# --- layout and environment ------------------------------------------------------------------


def test_the_reference_and_defect_builds_live_in_different_places(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "target": {"source_repo": tmp_path, "commit": "a" * 40},
            "root": tmp_path,
        }
    )
    layout = Layout.from_settings(settings)
    assert layout.source_dir(REFERENCE) == layout.sut_dir
    assert layout.source_dir("def-01") == layout.builds_root / "def-01"
    with pytest.raises(SandboxError):
        layout.source_dir("../escape")


def test_pythonpath_is_set_only_for_patched_builds() -> None:
    secrets = SandboxSecrets("s" * 64, "p" * 64, "e" * 64, {"A": ("a", "x"), "B": ("b", "y")})
    plain = build_env(
        secrets, Path(r"D:\db"), {"SYSTEMROOT": r"C:\Windows", "PYTHONPATH": r"C:\evil"}
    )
    assert "PYTHONPATH" not in plain
    patched = build_env(
        secrets, Path(r"D:\db"), {"SYSTEMROOT": r"C:\Windows"}, pythonpath=Path(r"D:\b\src")
    )
    assert patched["PYTHONPATH"] == r"D:\b\src"
