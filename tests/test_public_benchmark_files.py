"""The files the Agent may read: well-formed, consistent with each other, free of local details."""

import json
import re
from pathlib import Path

import pytest

PUBLIC = Path(__file__).resolve().parents[1] / "benchmarks" / "public"
FORBIDDEN = [
    r"[A-Za-z]:\\",  # absolute Windows paths
    r"/Users/|/home/",
    r"halfopen|baikai",  # real deployments
    r"\bsk-[A-Za-z0-9]",  # API key shapes
    r"eyJ[A-Za-z0-9_-]{10,}",  # JWT shapes
    r"@[A-Za-z0-9-]+\.[a-z]{2,}",  # e-mail addresses
]


def load(name: str) -> dict:
    return json.loads((PUBLIC / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def spec() -> dict:
    return load("openapi.json")


@pytest.fixture(scope="module")
def rules() -> dict:
    return load("rules.json")


@pytest.fixture(scope="module")
def notes() -> dict:
    return load("response-notes.json")


def operation_keys(spec: dict) -> set[str]:
    return {op["operationId"] for item in spec["paths"].values() for op in item.values()}


def test_spec_has_exactly_the_ten_in_scope_operations(spec: dict) -> None:
    assert operation_keys(spec) == {
        "accounts.list",
        "accounts.upsert",
        "accounts.archive",
        "holdings.list",
        "holdings.batch_upsert",
        "holdings.archive",
        "transactions.list",
        "transactions.batch_upsert",
        "transactions.archive",
        "ledger.summary",
    }


def test_spec_does_not_advertise_the_422_the_service_never_sends(spec: dict) -> None:
    for item in spec["paths"].values():
        for op in item.values():
            assert "422" not in op["responses"]


def test_every_schema_reference_in_the_spec_resolves(spec: dict) -> None:
    text = json.dumps(spec)
    for name in re.findall(r"#/components/schemas/(\w+)", text):
        assert name in spec["components"]["schemas"]


def test_rule_ids_are_unique_and_cover_the_three_flows(rules: dict) -> None:
    ids = [rule["id"] for rule in rules["rules"]]
    assert len(ids) == len(set(ids)) == 18
    assert {rule["flow"] for rule in rules["rules"]} == {"F1", "F2", "F3"}
    assert all(re.fullmatch(r"(LED|ARC|ISO)-\d{2}", rule_id) for rule_id in ids)


def test_every_rule_is_complete_and_only_uses_known_operations(rules: dict, spec: dict) -> None:
    known = operation_keys(spec)
    for rule in rules["rules"]:
        assert rule["statement"].strip() and rule["title"].strip() and rule["source"].strip()
        assert "owner_confirm" in rule, rule["id"]
        assert rule["operations"], rule["id"]
        assert set(rule["operations"]) <= known, rule["id"]


def test_rules_that_come_from_the_owner_say_what_needs_confirming(rules: dict) -> None:
    for rule in rules["rules"]:
        if rule["source"].startswith("owner"):
            assert rule["owner_confirm"], rule["id"]


def test_every_owner_confirmation_item_is_confirmed_with_a_date(rules: dict) -> None:
    """A rule with something for the owner to confirm may not be used until it is confirmed."""
    for rule in rules["rules"]:
        if rule["owner_confirm"]:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rule.get("confirmed_on", "")), rule["id"]
        else:
            assert "confirmed_on" not in rule, rule["id"]


def test_response_notes_describe_every_operation(notes: dict, spec: dict) -> None:
    assert set(notes["operations"]) == operation_keys(spec)
    assert {"Account", "Holding", "Transaction", "ArchiveResult", "LedgerSummary"} <= set(
        notes["objects"]
    )


@pytest.mark.parametrize("name", ["openapi.json", "rules.json", "response-notes.json"])
def test_public_files_contain_no_local_or_secret_looking_details(name: str) -> None:
    text = (PUBLIC / name).read_text(encoding="utf-8")
    for pattern in FORBIDDEN:
        assert not re.search(pattern, text), f"{name} matches {pattern}"


@pytest.mark.parametrize("name", ["openapi.json", "rules.json", "response-notes.json"])
def test_public_files_are_lf_utf8_with_a_final_newline(name: str) -> None:
    raw = (PUBLIC / name).read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    raw.decode("utf-8")
