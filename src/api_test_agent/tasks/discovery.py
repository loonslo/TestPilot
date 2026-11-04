"""Snapshot discovery with explicit URL import. Never execute target Python modules."""

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

from api_test_agent.adapters import (
    StrictLoader,
    get_adapter,
    get_all_supported_extensions,
    openapi,
)
from api_test_agent.tasks.fastapi_static import extract
from api_test_agent.tasks.models import Contract, Operation, SourceRef, Task
from api_test_agent.tasks.openapi_import import fetch_openapi_document
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
DEFAULT_PERMITTED_EXTENSIONS = {".py", ".json", ".yaml", ".yml", ".md", ".txt"}

# Sentinels for materials not bound to the selected commit. A locally supplied
# document cannot be tied to the source revision, so it must never be reported
# with the target commit as its version.
UNVERIFIED_DOCUMENT_VERSION = "unverified-document"
MIXED_VERSION_WARNING = (
    "Document/source version correspondence unverified: local document is not "
    "bound to the selected commit"
)


def get_permitted_extensions() -> set[str]:
    return DEFAULT_PERMITTED_EXTENSIONS | get_all_supported_extensions()


def permitted(path: str, extra_extensions: set[str] | None = None) -> bool:
    parts = Path(path).parts
    allowed = (
        get_permitted_extensions()
        if extra_extensions is None
        else (get_permitted_extensions() | extra_extensions)
    )
    return (
        not any(p.startswith(".") or p.lower() in DENIED for p in parts)
        and not any(p.startswith("test_") for p in parts)
        and Path(path).suffix.lower() in allowed
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
    all_materials_version_verified: bool = False
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
    commit_paths: set[str] = set()
    document_paths: set[str] = set()
    imported_documents: dict[str, str] = {}
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
            commit_paths.add(path)
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
            # A locally supplied document is not bound to the selected commit.
            # Annotate it so it can never masquerade as a commit-verified source.
            document_paths.add(path)
            checkpoint(path, content)
    if task.openapi_url:
        path = f"url-import/{hashlib.sha256(task.openapi_url.encode()).hexdigest()}.json"
        if path in snapshots:
            raise ValueError("OpenAPI URL virtual path conflicts with selected material")
        content = fetch_openapi_document(task.openapi_url)
        parsed = yaml.load(content, Loader=StrictLoader)
        if not isinstance(parsed, dict) or not ("openapi" in parsed or "swagger" in parsed):
            raise ValueError("URL response is not an OpenAPI/Swagger document")
        snapshots[path] = content
        document_paths.add(path)
        imported_documents[path] = task.openapi_url
        checkpoint(path, content)
    sources = [
        ref(
            path,
            content,
            task.target_commit
            if path in commit_paths and path not in document_paths
            else UNVERIFIED_DOCUMENT_VERSION,
        )
        for path, content in sorted(snapshots.items())
    ]
    sources = [
        source.model_copy(
            update={"summary": f"OpenAPI URL import: {imported_documents[source.path]}"}
        )
        if source.path in imported_documents
        else source
        for source in sources
    ]
    sources_map = {source.path: source for source in sources}
    adapter = get_adapter(task.adapter) if getattr(task, "adapter", None) else None
    adapter_extensions: set[str] = set()
    if adapter is not None:
        code_rows, code_notes = adapter.extract(snapshots, sources_map)
        adapter_extensions = {ext.lower() for ext in adapter.supported_extensions}
    else:
        code_rows, code_notes = extract(snapshots, sources_map)
    warnings.extend(code_notes)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in code_rows:
        grouped.setdefault(f"{row['method']} {row['path']}", []).append(row)
    for source in sources:
        content = snapshots[source.path]
        if source.path.endswith(".py"):
            continue
        elif Path(source.path).suffix.lower() in adapter_extensions:
            # The selected adapter already parsed this document; do not parse it again.
            continue
        elif Path(source.path).suffix in {".yaml", ".yml", ".json"}:
            rows, notes = openapi(content, source, snapshots, sources_map)
        else:
            rows, notes = [], [f"{source.path}: prose retained; no automatic interface extraction"]
        warnings.extend(notes)
        for row in rows:
            key = f"{row['method']} {row['path']}"
            grouped.setdefault(key, []).append(row)
    operations = []
    all_rows = [r for rows in grouped.values() for r in rows]
    has_document_routes = any(r.get("details", {}).get("kind") == "requirement" for r in all_rows)
    has_code_routes = any(r.get("details", {}).get("kind") == "implementation" for r in all_rows)
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
        if len(rows) > 1 and len(signatures) > 1:
            row["status"] = "conflict"
            row["reason"] = (
                "Multiple divergent declarations require reconciliation; "
                f"{len(signatures)} distinct declaration signatures"
            )
        elif len(rows) > 1 and row["status"] != "out_of_scope":
            row["reason"] += f"; {len(rows)} consistent declarations of this operation"
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
    elif document_paths:
        # Mixed inventory: source files come from the commit, but at least one
        # locally supplied document is not bound to the selected revision.
        warnings.append(MIXED_VERSION_WARNING)
    all_materials_version_verified = bool(verified and not document_paths)
    warnings.append("Static inventory is not proof of complete runtime route coverage")
    inventory = Inventory(
        target_commit=task.target_commit,
        version_verified=all_materials_version_verified,
        all_materials_version_verified=all_materials_version_verified,
        materials=tuple(sources),
        operations=tuple(operations),
        limitations=tuple(warnings),
        snapshots=snapshots,
        task_hash=task_binding(task),
        input_root_hash=root_binding(materials_root),
    )
    path = store.folder(task_id) / "inventory.json"
    _write_new(path, inventory)
    store.transition(task_id, "discovered", "Inventory snapshot saved; limitations retained")
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
    if data.get("openapi_url") is None:
        data.pop("openapi_url", None)  # Preserve bindings for tasks predating URL imports.
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def root_binding(root: Path | None) -> str | None:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest() if root else None


def discover(store: TaskStore, task_id: str, materials_root: Path | None = None) -> Path:
    task = store.load(task_id)
    task.require_target()
    if task.status != "created":
        raise ValueError("discovery requires a created task")
    inventory_path = store.folder(task_id) / "inventory.json"
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
    attempt = store.folder(task_id) / "discovery" / uuid4().hex
    attempt.mkdir(parents=True)
    metadata = {
        "task_hash": task_binding(task),
        "input_root_hash": root_binding(materials_root),
        "started_at": datetime.now(UTC).isoformat(),
    }
    (attempt / "started.json").write_text(json.dumps(metadata, indent=2), "utf-8")

    def checkpoint(path: str, content: str) -> None:
        is_document = any(
            path == selection or path.startswith(selection.rstrip("/") + "/")
            for selection in task.document_paths or ()
        )
        version = (
            UNVERIFIED_DOCUMENT_VERSION
            if is_document or path.startswith("url-import/")
            else task.target_commit
        )
        source = ref(path, content, version)
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
