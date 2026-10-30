"""Offline, allowlisted discovery. Never import or execute target Python modules."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import ConfigDict, model_validator

from api_test_agent.tasks.fastapi_static import extract
from api_test_agent.tasks.models import Contract, Operation, SourceRef, Task
from api_test_agent.tasks.store import TaskStore, _write_new

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
DENIED = {
    "data",
    "runtime",
    "tests",
    "test",
    "benchmarks",
    "private",
    "node_modules",
    "backups",
    "backup",
    "logs",
    "__pycache__",
}


def permitted(path: str) -> bool:
    parts = Path(path).parts
    return (
        not any(p.startswith(".") or p.lower() in DENIED for p in parts)
        and not any(p.startswith("test_") for p in parts)
        and Path(path).suffix.lower() in {".py", ".json", ".yaml", ".yml", ".md", ".txt"}
        and not re.search(r"(?i)(credential|secret|token|password|cookie|\.pem$|\.key$)", path)
    )


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("cannot read selected Git snapshot")
    return result.stdout


def ref(path: str, content: str, version: str) -> SourceRef:
    return SourceRef(
        source_id=hashlib.sha256(path.encode()).hexdigest()[:16],
        version=version,
        path=path,
        locator="full file",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        summary="User-selected offline material",
    )


class Inventory(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    target_commit: str
    version_verified: bool
    materials: tuple[SourceRef, ...]
    operations: tuple[Operation, ...]
    limitations: tuple[str, ...]
    snapshots: dict[str, str]
    task_hash: str | None = None
    input_root_hash: str | None = None

    @model_validator(mode="after")
    def verify_materials(self) -> Inventory:
        refs = {source.path: source for source in self.materials}
        if len(refs) != len(self.materials) or set(refs) != set(self.snapshots):
            raise ValueError("inventory sources and snapshot files differ")
        for path, source in refs.items():
            if hashlib.sha256(self.snapshots[path].encode()).hexdigest() != source.content_hash:
                raise ValueError("inventory content does not match source hash")
        exact_sources = {source.source_id: source for source in self.materials}
        ids = set(exact_sources)
        if len(ids) != len(refs):
            raise ValueError("duplicate source IDs")
        if any(exact_sources.get(s.source_id) != s for op in self.operations for s in op.sources):
            raise ValueError("operation references an unknown source")
        return self


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader: StrictLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, (str, int)):
            raise ValueError("document mapping keys must be strings or integer status codes")
        key = str(key)
        if key in result:
            raise ValueError("duplicate document mapping key; reconcile declarations explicitly")
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def resolve_refs(value: Any, root: dict, seen: frozenset[str] = frozenset(), depth: int = 0) -> Any:
    if depth > 32:
        return {"$ref": "unresolved-depth-limit"}
    if isinstance(value, list):
        return [resolve_refs(item, root, seen, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str):
        if not reference.startswith("#/") or reference in seen:
            return value
        target: Any = root
        try:
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except KeyError, TypeError:
            return value
        merged = resolve_refs(target, root, seen | {reference}, depth + 1)
        if not isinstance(merged, dict):
            return value
        return merged | {
            k: resolve_refs(v, root, seen, depth + 1) for k, v in value.items() if k != "$ref"
        }
    return {k: resolve_refs(v, root, seen, depth + 1) for k, v in value.items()}


def openapi(content: str, source: SourceRef) -> tuple[list[dict[str, Any]], list[str]]:
    data = yaml.load(content, Loader=StrictLoader)
    if not isinstance(data, dict) or not ("openapi" in data or "swagger" in data):
        return [], [f"{source.path}: no OpenAPI document; retained as rule material"]
    warnings: list[str] = []
    rows = []
    prefix = data.get("basePath", "")
    if not isinstance(prefix, str):
        raise ValueError("OpenAPI basePath must be text")
    servers = data.get("servers", [])
    if not isinstance(servers, list) or any(not isinstance(server, dict) for server in servers):
        raise ValueError("OpenAPI servers must be a list of objects")
    ambiguous_server = False
    if servers:
        from urllib.parse import urlsplit

        prefix = urlsplit(str(servers[0].get("url", ""))).path.rstrip("/")
        if len(servers) > 1 or "{" in prefix:
            ambiguous_server = True
            warnings.append(f"{source.path}: multiple/variable servers need confirmation")
    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI paths must be a mapping")
    for path, item in paths.items():
        item = resolve_refs(item, data)
        if not isinstance(item, dict):
            warnings.append(f"{source.path}: malformed path {path}")
            continue
        if "$ref" in item:
            warnings.append(f"{source.path}: unresolved path reference {path}")
        for method, detail in item.items():
            if method not in METHODS:
                continue
            if not isinstance(detail, dict):
                warnings.append(f"{source.path}: malformed operation {method} {path}")
                continue
            detail = dict(detail)
            if (
                not isinstance(item.get("parameters", []), list)
                or not isinstance(detail.get("parameters", []), list)
                or not isinstance(detail.get("responses", {}), dict)
            ):
                raise ValueError("OpenAPI parameters/responses have invalid shapes")
            detail["parameters"] = [*item.get("parameters", []), *detail.get("parameters", [])]
            full_path = prefix.rstrip("/") + "/" + str(path).lstrip("/")
            unsupported = any(k in json.dumps(detail) for k in ("multipart/", "text/event-stream"))
            rows.append(
                dict(
                    method=method.upper(),
                    path=full_path,
                    original_operation_ids=[str(detail.get("operationId", ""))],
                    mount_prefix=prefix,
                    sources=[source],
                    status=(
                        "unsupported"
                        if unsupported
                        else "unresolved"
                        if "$ref" in json.dumps(detail) or ambiguous_server
                        else "confirmed"
                    ),
                    reason="Document declaration; runtime not checked",
                    details={
                        "kind": "requirement",
                        "parameters": detail.get("parameters", []),
                        "authentication": detail.get("security", data.get("security", [])),
                        "responses": detail.get("responses", {}),
                        "request_body": detail.get("requestBody"),
                        "locator": f"paths.{path}.{method}",
                        "raw": detail,
                    },
                    signature=json.dumps(detail, sort_keys=True),
                )
            )
    return rows, warnings


def _discover(
    store: TaskStore,
    task_id: str,
    materials_root: Path | None,
    checkpoint: Callable[[str, str], None],
) -> Path:
    task = store.load(task_id)
    task.require_target()
    if task.status != "created":
        raise ValueError("discovery requires a created task")
    snapshots: dict[str, str] = {}
    warnings: list[str] = []
    verified = False
    if task.source_paths:
        if not task.repository:
            raise ValueError("source selection requires repository")
        repo = Path(task.repository).resolve()
        actual = (
            git(repo, "rev-parse", "--verify", f"{task.target_commit}^{{commit}}").decode().strip()
        )
        if actual != task.target_commit:
            raise ValueError("selected commit does not resolve exactly")
        verified = True
        listing = git(repo, "ls-tree", "-r", "-z", task.target_commit).split(b"\0")
        matches = {selection: 0 for selection in task.source_paths}
        for entry in listing:
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            path = name.decode("utf-8")
            selected = [s for s in matches if path == s or path.startswith(s.rstrip("/") + "/")]
            if not selected:
                continue
            for selection in selected:
                matches[selection] += 1
            if not permitted(path) or not metadata.startswith(b"100"):
                warnings.append(f"{path}: excluded or non-regular Git entry")
                continue
            blob = metadata.split()[-1].decode()
            size = int(git(repo, "cat-file", "-s", blob))
            if size > 1_000_000:
                warnings.append(f"{path}: file exceeds snapshot limit")
                continue
            snapshots[path] = git(repo, "cat-file", "blob", blob).decode("utf-8")
            checkpoint(path, snapshots[path])
        warnings += [
            f"{s}: no committed files matched" for s, count in matches.items() if not count
        ]
    if task.document_paths:
        if materials_root is None:
            raise ValueError("document selection requires --materials-root")
        root = materials_root.resolve()
        for path in task.document_paths:
            candidate = (root / path).resolve()
            if not candidate.is_relative_to(root) or not permitted(path):
                raise ValueError("document path excluded or escapes material root")
            if candidate.stat().st_size > 1_000_000:
                raise ValueError("document exceeds snapshot limit")
            content = candidate.read_text("utf-8")
            if path in snapshots and snapshots[path] != content:
                raise ValueError("document and committed source share a conflicting path")
            snapshots[path] = content
            checkpoint(path, content)
    sources = [
        ref(path, content, task.target_commit) for path, content in sorted(snapshots.items())
    ]
    code_rows, code_notes = extract(snapshots, {source.path: source for source in sources})
    warnings.extend(code_notes)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in code_rows:
        grouped.setdefault(f"{row['method']} {row['path']}", []).append(row)
    for source in sources:
        content = snapshots[source.path]
        if source.path.endswith(".py"):
            continue
        elif Path(source.path).suffix in {".yaml", ".yml", ".json"}:
            rows, notes = openapi(content, source)
        else:
            rows, notes = [], [f"{source.path}: prose retained; no automatic interface extraction"]
        warnings.extend(notes)
        for row in rows:
            key = f"{row['method']} {row['path']}"
            grouped.setdefault(key, []).append(row)
    operations = []
    has_document_routes = any(
        r.get("details", {}).get("kind") == "requirement" for rows in grouped.values() for r in rows
    )
    has_code_routes = bool(code_rows)
    for key, rows in sorted(grouped.items()):
        row = dict(rows[0])
        row["declarations"] = tuple(
            dict(r.get("details", {}), source_ids=[s.source_id for s in r["sources"]]) for r in rows
        )
        row.pop("details", None)
        signatures = {r.pop("signature") for r in rows}
        row.pop("signature", None)
        row["operation_id"] = hashlib.sha256(key.encode()).hexdigest()[:16]
        row["sources"] = list({s.source_id: s for r in rows for s in r["sources"]}.values())
        row["original_operation_ids"] = sorted(
            {i for r in rows for i in r["original_operation_ids"] if i}
        )
        if len(rows) > 1:
            row["status"] = "conflict"
            row["reason"] = (
                "Multiple declarations require reconciliation; "
                f"{len(signatures)} distinct declaration signatures"
            )
        differences = reconcile(rows)
        kinds = {r.get("details", {}).get("kind") for r in rows}
        if has_document_routes and has_code_routes and len(kinds) == 1:
            differences.append(
                "only_in_document" if "requirement" in kinds else "only_in_implementation"
            )
            if row["status"] == "confirmed":
                row["status"] = "unresolved"
            row["reason"] += "; missing from other supplied declaration source"
        row["differences"] = tuple(differences)
        if not any(fnmatch.fnmatchcase(key, pattern) for pattern in task.scope):
            row["status"] = "out_of_scope"
            row["reason"] = "Not selected by task METHOD /path glob scope"
        operations.append(Operation(**row))
    if not verified:
        warnings.append("Document-only inventory: target version and runtime are unverified")
    warnings.append("Static inventory is not proof of complete runtime route coverage")
    inventory = Inventory(
        target_commit=task.target_commit,
        version_verified=verified,
        materials=tuple(sources),
        operations=tuple(operations),
        limitations=tuple(warnings),
        snapshots=snapshots,
        task_hash=task_binding(task),
        input_root_hash=root_binding(materials_root),
    )
    path = store._folder(task_id) / "inventory.json"
    _write_new(path, inventory)
    store.transition(task_id, "discovered", "Offline inventory saved; limitations retained")
    return path


def reconcile(rows: list[dict]) -> list[str]:
    """Compare only statically knowable facts; keep all raw declarations for review."""
    docs = [r["details"] for r in rows if r.get("details", {}).get("kind") == "requirement"]
    codes = [r["details"] for r in rows if r.get("details", {}).get("kind") == "implementation"]
    differences: list[str] = []
    types = {"int": "integer", "float": "number", "str": "string", "bool": "boolean"}
    for document in docs:
        for code in codes:
            declared = {
                p.get("name"): p
                for p in document.get("parameters", [])
                if isinstance(p, dict) and "name" in p
            }
            implemented = {p["name"]: p for p in code.get("parameters", [])}
            for name, parameter in declared.items():
                actual = implemented.get(name)
                if actual is None:
                    differences.append(f"parameter_not_found_in_signature:{name}")
                    continue
                annotation = actual.get("annotation")
                expected_type = parameter.get("schema", {}).get("type")
                if annotation in types and expected_type and types[annotation] != expected_type:
                    differences.append(
                        f"parameter_type:{name}:{expected_type}!={types[annotation]}"
                    )
                if actual.get("default") in {None, "None"} and bool(
                    parameter.get("required", False)
                ) != actual.get("required"):
                    differences.append(f"parameter_required:{name}")
            if code.get("authentication", "").startswith("unresolved"):
                differences.append("authentication_requires_confirmation")
            options = code.get("options", {})
            status = options.get("status_code")
            if status and status.isdigit() and status not in document.get("responses", {}):
                differences.append(f"response_status_not_documented:{status}")
    return sorted(set(differences))


def task_binding(task: Task) -> str:
    data = task.model_dump(mode="json", exclude={"status", "history"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def root_binding(root: Path | None) -> str | None:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest() if root else None


def discover(store: TaskStore, task_id: str, materials_root: Path | None = None) -> Path:
    task = store.load(task_id)
    task.require_target()
    if task.status != "created":
        raise ValueError("discovery requires a created task")
    inventory_path = store._folder(task_id) / "inventory.json"
    if inventory_path.exists():
        inventory = Inventory.model_validate_json(inventory_path.read_text("utf-8"))
        if inventory.task_hash != task_binding(task) or inventory.input_root_hash != root_binding(
            materials_root
        ):
            raise ValueError("existing inventory belongs to different input settings")
        store.transition(
            task_id, "discovered", "Recovered complete inventory after interrupted save"
        )
        return inventory_path
    attempt = store._folder(task_id) / "discovery" / uuid4().hex
    attempt.mkdir(parents=True)
    metadata = {
        "task_hash": task_binding(task),
        "input_root_hash": root_binding(materials_root),
        "started_at": datetime.now(UTC).isoformat(),
    }
    (attempt / "started.json").write_text(json.dumps(metadata, indent=2), "utf-8")

    def checkpoint(path: str, content: str) -> None:
        source = ref(path, content, task.target_commit)
        payload = {"source": source.model_dump(mode="json"), "content": content}
        snapshot_path = attempt / f"{source.source_id}.json"
        if snapshot_path.exists():
            return
        with snapshot_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False))

    try:
        result = _discover(store, task_id, materials_root, checkpoint)
    except (OSError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        # Do not persist exception text: parser excerpts can contain private input.
        (attempt / "failed.json").write_text(
            json.dumps(
                {
                    "error_type": type(exc).__name__,
                    "reason": "Discovery stopped; inspect captured materials and selected paths",
                    "at": datetime.now(UTC).isoformat(),
                }
            ),
            "utf-8",
        )
        raise
    (attempt / "completed.json").write_text(
        json.dumps({"inventory": "inventory.json", "at": datetime.now(UTC).isoformat()}), "utf-8"
    )
    return result


def model_materials(inventory: Inventory, max_chars: int) -> str:
    """Bound the actual serialized payload, recording every unsent material."""
    base = {"target_commit": inventory.target_commit, "materials": [], "omitted": []}
    for source in inventory.materials:
        item = {
            "source": source.model_dump(mode="json"),
            "content": inventory.snapshots[source.path],
        }
        base["materials"].append(item)

    def encode() -> str:
        return json.dumps(base, ensure_ascii=False, separators=(",", ":"))

    while len(encode()) > max_chars and base["materials"]:
        item = base["materials"].pop()
        base["omitted"].append(item["source"]["source_id"])
    if len(encode()) > max_chars:
        raise ValueError("budget cannot hold provenance/omission manifest")
    return encode()
