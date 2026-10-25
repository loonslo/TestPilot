"""Build the Agent-facing OpenAPI subset from the reference build's runtime export.

Host-side tool. It starts a scenario on the exported reference build, downloads ``/openapi.json``
(the committed snapshot in the target repository is stale), keeps only the in-scope operations,
gives them stable short keys, and drops the generic 422 entries because the service really answers
validation errors with 400 and its own error envelope. Run from the project root:

    uv run python tools/build_public_spec.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from api_test_agent import sandbox
from api_test_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "benchmarks" / "public" / "openapi.json"

# (method, path) -> stable key used by the Agent's tools
OPERATIONS: dict[tuple[str, str], str] = {
    ("get", "/api/v1/accounts"): "accounts.list",
    ("post", "/api/v1/accounts"): "accounts.upsert",
    ("post", "/api/v1/accounts/{account_id}:archive"): "accounts.archive",
    ("get", "/api/v1/holdings"): "holdings.list",
    ("post", "/api/v1/holdings:batch-upsert"): "holdings.batch_upsert",
    ("post", "/api/v1/holdings/{holding_id}:archive"): "holdings.archive",
    ("get", "/api/v1/transactions"): "transactions.list",
    ("post", "/api/v1/transactions:batch-upsert"): "transactions.batch_upsert",
    ("post", "/api/v1/transactions/{transaction_id}:archive"): "transactions.archive",
    ("get", "/api/v1/ledger/summary"): "ledger.summary",
}


def referenced_schemas(node: Any, schemas: dict[str, Any], found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[1]
            if name not in found:
                found.add(name)
                referenced_schemas(schemas[name], schemas, found)
        for value in node.values():
            referenced_schemas(value, schemas, found)
    elif isinstance(node, list):
        for value in node:
            referenced_schemas(value, schemas, found)


def build(full: dict[str, Any]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for (method, path), key in OPERATIONS.items():
        operation = copy.deepcopy(full["paths"][path][method])
        operation["x-source-operation-id"] = operation.pop("operationId")
        operation["operationId"] = key
        operation["responses"].pop("422", None)
        operation.pop("tags", None)
        paths.setdefault(path, {})[method] = operation
    schemas = full["components"]["schemas"]
    found: set[str] = set()
    referenced_schemas(paths, schemas, found)
    return {
        "openapi": full["openapi"],
        "info": {
            "title": full["info"]["title"],
            "version": full["info"]["version"],
            "description": (
                "In-scope subset of the service's OpenAPI. Response bodies are described in "
                "response-notes.json because the service does not type them."
            ),
        },
        "paths": dict(sorted(paths.items())),
        "components": {
            "schemas": {name: schemas[name] for name in sorted(found)},
            "securitySchemes": full["components"].get("securitySchemes", {}),
        },
    }


def main() -> int:
    settings = Settings.load(ROOT / "config.local.toml")
    scenario = sandbox.start_scenario(settings, "spec-1")
    try:
        with sandbox.target_client(scenario.base_url) as client:
            full = client.get("/openapi.json").json()
    finally:
        sandbox.stop_scenario(settings, scenario)
    public = build(full)
    text = json.dumps(public, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(
        f"runtime export: {len(full['paths'])} paths; wrote {OUTPUT.relative_to(ROOT)} "
        f"({len(public['paths'])} paths, {len(OPERATIONS)} operations, "
        f"{len(public['components']['schemas'])} schemas); sha256 {digest[:16]}"
    )
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
