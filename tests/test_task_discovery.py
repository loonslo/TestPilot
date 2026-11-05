import hashlib
import json
import subprocess

import pytest

from api_test_agent.cli import main
from api_test_agent.tasks.discovery import Inventory, discover, model_materials
from api_test_agent.tasks.models import Task
from api_test_agent.tasks.store import TaskStore


def command(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return result.stdout.decode().strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    command(repo, "init")
    command(repo, "config", "user.email", "synthetic@example.invalid")
    command(repo, "config", "user.name", "Synthetic fixture")
    (repo / "api.py").write_text(
        """from fastapi import FastAPI, APIRouter
app = FastAPI()
router = APIRouter(prefix="/v1")
@router.get("/items")
def items(): pass
app.include_router(router, prefix="/api")
@app.get(dynamic_path)
def dynamic(): pass
""",
        encoding="utf-8",
    )
    (repo / ".env").write_text("SYNTHETIC_EXCLUDED_MARKER=not-a-secret", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_api.py").write_text("# Hidden existing tests", encoding="utf-8")
    command(repo, "add", ".")
    command(repo, "commit", "-m", "Synthetic source")
    return repo, command(repo, "rev-parse", "HEAD")


def document(root, name="api.json", status="OK"):
    data = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://example.invalid/v1"}],
        "paths": {
            "/items": {
                "get": {"operationId": "listItems", "responses": {"200": {"description": status}}}
            },
            "/upload": {"post": {"requestBody": {"content": {"multipart/form-data": {}}}}},
        },
    }
    (root / name).write_text(json.dumps(data), encoding="utf-8")


def test_snapshot_uses_commit_and_does_not_touch_worktree(tmp_path, repository):
    repo, commit = repository
    # Select only known allowed source; dirty worktree is not analysis input.
    (repo / "api.py").write_text("raise RuntimeError('must never execute')", encoding="utf-8")
    before = command(repo, "status", "--porcelain")
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit=commit,
            scope=["*"],
            repository=str(repo),
            source_paths=["api.py", "tests"],
        )
    )
    path = discover(store, "demo")
    inventory = Inventory.model_validate_json(path.read_text("utf-8"))
    assert inventory.version_verified
    assert inventory.all_materials_version_verified
    assert "router = APIRouter" in inventory.snapshots["api.py"]
    assert set(inventory.snapshots) == {"api.py"}
    assert (
        inventory.materials[0].content_hash
        == hashlib.sha256(inventory.snapshots["api.py"].encode()).hexdigest()
    )
    assert inventory.operations[0].key == "GET /api/v1/items"
    assert inventory.operations[0].status == "confirmed"
    assert inventory.operations[0].mount_prefix == "/api/v1"
    assert any("dynamic expression" in note for note in inventory.limitations)
    assert any("excluded" in note for note in inventory.limitations)
    assert "SYNTHETIC_EXCLUDED_MARKER" not in inventory.model_dump_json()
    assert store.load("demo").status == "discovered"
    assert command(repo, "status", "--porcelain") == before
    with pytest.raises(ValueError):
        store.configure("demo", target_commit="a" * 40)


def test_mixed_source_and_document_marks_version_unverified(tmp_path, repository):
    repo, commit = repository
    document(tmp_path, "api.json")
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="mixed",
            target_project="synthetic",
            target_commit=commit,
            scope=["*"],
            repository=str(repo),
            source_paths=["api.py"],
            document_paths=["api.json"],
        )
    )
    inv = Inventory.model_validate_json(discover(store, "mixed", tmp_path).read_text("utf-8"))
    # The git source is bound to the commit, but the local document is not.
    assert not inv.version_verified
    assert not inv.all_materials_version_verified
    assert any("Document/source version correspondence" in n for n in inv.limitations)
    materials = {m.path: m for m in inv.materials}
    assert materials["api.py"].version == commit
    # The locally supplied document must not masquerade as commit-verified.
    assert materials["api.json"].version != commit
    assert materials["api.json"].version == "unverified-document"
    # Both kinds still surface as operations.
    assert {"GET /v1/items"} <= {op.key for op in inv.operations}


@pytest.mark.parametrize("adapter", [None, "openapi"])
def test_discovery_resolves_selected_cross_file_documents(tmp_path, adapter):
    (tmp_path / "api.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {"/items": {"post": {"requestBody": {"$ref": "body.json#/body"}}}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "body.json").write_text(
        json.dumps({"body": {"content": {"application/json": {"schema": {"type": "string"}}}}}),
        encoding="utf-8",
    )
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="cross",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json", "body.json"],
            adapter=adapter,
        )
    )
    inv = Inventory.model_validate_json(discover(store, "cross", tmp_path).read_text("utf-8"))
    assert not inv.all_materials_version_verified
    assert len(inv.operations) == 1
    operation = inv.operations[0]
    assert operation.status == "confirmed"
    assert len(operation.declarations) == 1
    assert {s.path for s in operation.sources} == {"api.json", "body.json"}
    assert {s.version for s in operation.sources} == {"unverified-document"}


def test_document_conflict_scope_and_unsupported_kept(tmp_path):
    document(tmp_path)
    document(tmp_path, "other.json", "Changed description")
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["GET *"],
            document_paths=["api.json", "other.json"],
        )
    )
    inv = Inventory.model_validate_json(discover(store, "demo", tmp_path).read_text("utf-8"))
    assert not inv.version_verified
    assert {op.key: op.status for op in inv.operations} == {
        "GET /v1/items": "conflict",
        "POST /v1/upload": "out_of_scope",
    }
    assert len(inv.operations[0].sources) == 2
    assert any("Document-only" in note for note in inv.limitations)
    store.create(
        Task(
            task_id="all",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json"],
        )
    )
    all_inv = Inventory.model_validate_json(discover(store, "all", tmp_path).read_text("utf-8"))
    assert all_inv.operations[1].status == "unsupported"


def test_actual_serialized_materials_budget_and_provenance(tmp_path):
    document(tmp_path)
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json"],
        )
    )
    inv = Inventory.model_validate_json(discover(store, "demo", tmp_path).read_text("utf-8"))
    full = model_materials(inv, 5000)
    payload = json.loads(full)
    assert payload["materials"][0]["content"] == inv.snapshots["api.json"]
    assert payload["materials"][0]["source"]["content_hash"] == inv.materials[0].content_hash
    bounded = model_materials(inv, 200)
    assert len(bounded) <= 200
    assert json.loads(bounded)["omitted"] == [inv.materials[0].source_id]
    with pytest.raises(ValueError):
        model_materials(inv, 5)


def test_discovery_refuses_incomplete_input_and_fake_state(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    store.create(Task(task_id="draft"))
    with pytest.raises(ValueError):
        discover(store, "draft")
    assert not (tmp_path / "tasks/draft/inventory.json").exists()
    store.configure(
        "draft",
        target_project="synthetic",
        target_commit="a" * 40,
        scope=["*"],
        document_paths=["missing.json"],
    )
    with pytest.raises(ValueError):
        store.transition("draft", "discovered", "Pretend discovery happened")
    assert (
        main(
            [
                "task",
                "--root",
                str(tmp_path / "tasks"),
                "discover",
                "draft",
                "--materials-root",
                str(tmp_path),
            ]
        )
        == 2
    )
    assert store.load("draft").status == "created"


def test_document_parent_escape_rejected(tmp_path):
    # Reject traversal at registration, before discovery can read content.
    from api_test_agent.tasks.models import Task

    with pytest.raises(ValueError):
        Task(task_id="demo", document_paths=["../outside.json"])
    with pytest.raises(ValueError):
        Task(task_id="demo", document_paths=["C:/docs/../outside.json"])


def test_external_absolute_document_is_snapshotted_under_virtual_path(tmp_path):
    external = tmp_path / "outside" / "apple-docs"
    external.mkdir(parents=True)
    document(external, "swagger.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = TaskStore(workspace / "tasks")
    store.create(
        Task(
            task_id="external",
            target_project="AppleRecruit",
            scope=["*"],
            document_paths=[str(external / "swagger.json")],
            adapter="openapi",
        )
    )
    inv = Inventory.model_validate_json(discover(store, "external", workspace).read_text("utf-8"))
    assert set(inv.snapshots) == {"external/swagger.json"}
    assert "openapi" in inv.snapshots["external/swagger.json"]
    material = inv.materials[0]
    assert material.version == "unverified-document"
    assert str(external / "swagger.json").replace("\\", "/") in material.summary
    assert inv.operations
    assert any("external document read from" in note for note in inv.limitations)


def test_document_under_runtime_directory_is_allowed(tmp_path):
    runtime = tmp_path / "runtime" / "demo"
    runtime.mkdir(parents=True)
    document(runtime, "openapi.json")
    store = TaskStore(tmp_path / "tasks")
    # A document-only task needs no declared commit: its version stays unverified.
    store.create(
        Task(
            task_id="runtime-doc",
            target_project="synthetic",
            scope=["*"],
            document_paths=["runtime/demo/openapi.json"],
            adapter="openapi",
        )
    )
    inv = Inventory.model_validate_json(discover(store, "runtime-doc", tmp_path).read_text("utf-8"))
    assert "runtime/demo/openapi.json" in inv.snapshots
    assert inv.materials[0].version == "unverified-document"


def test_local_document_limit_matches_url_import(tmp_path, monkeypatch):
    import api_test_agent.tasks.discovery as discovery_module

    # The URL route allows 20 MB per document; a local file must not be capped lower.
    assert discovery_module.MAX_DOCUMENT_BYTES == 20_000_000
    monkeypatch.setattr(discovery_module, "MAX_DOCUMENT_BYTES", 10)
    (tmp_path / "big.json").write_text("x" * 100, encoding="utf-8")
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="big-doc",
            target_project="synthetic",
            scope=["*"],
            document_paths=["big.json"],
            adapter="openapi",
        )
    )
    with pytest.raises(ValueError, match="exceeds snapshot limit"):
        discover(store, "big-doc", tmp_path)


def extract_graph(files):
    from api_test_agent.tasks.discovery import ref
    from api_test_agent.tasks.fastapi_static import extract

    sources = {path: ref(path, content, "a" * 40) for path, content in files.items()}
    return extract(files, sources)


def test_cross_module_aliases_nested_mounts_and_repeated_router():
    rows, notes = extract_graph(
        {
            "src/app/main.py": """from fastapi import FastAPI as Application
from app.routes import router as item_router
from app.child import subapp
app = Application()
app.include_router(item_router, prefix="/first")
app.include_router(item_router, prefix="/second")
app.mount("/child", subapp)
""",
            "src/app/routes.py": """from fastapi import APIRouter
from .nested import nested
router = APIRouter(prefix="/v1")
router.include_router(nested, prefix="/inner")
""",
            "src/app/nested.py": """import fastapi as f
nested = f.APIRouter(prefix="/items")
@nested.api_route("/{item_id}", methods=["GET", "DELETE"])
async def item(item_id: int): pass
""",
            "src/app/child.py": """from fastapi import FastAPI
subapp = FastAPI()
@subapp.post("/create", status_code=201)
def create(): pass
""",
        }
    )
    assert not notes
    assert {(r["method"], r["path"]) for r in rows} == {
        (method, prefix + "/v1/inner/items/{item_id}")
        for method in ("GET", "DELETE")
        for prefix in ("/first", "/second")
    } | {("POST", "/child/create")}
    nested = next(r for r in rows if r["method"] == "DELETE")
    assert len(nested["sources"]) == 3
    assert nested["details"]["parameters"][0]["annotation"] == "int"
    assert all(r["status"] == "confirmed" for r in rows)


def test_cycles_orphans_dynamic_and_websockets_remain_visible():
    rows, notes = extract_graph(
        {
            "api.py": """from fastapi import FastAPI, APIRouter
app = FastAPI()
orphan = APIRouter(prefix="/orphan")
orphan.include_router(orphan)
@orphan.get("/")
def orphaned(): pass
@app.websocket("/socket")
async def socket(): pass
app.include_router(outside_selection)
@app.get(build_path())
def dynamic(): pass
if enabled:
    app.include_router(orphan)
"""
        }
    )
    assert any("cyclic" in note for note in notes)
    assert any("control flow" in note for note in notes)
    assert any("unresolved mounted" in note for note in notes)
    assert any("dynamic expression" in note for note in notes)
    assert next(r for r in rows if r["path"] == "/orphan/")["status"] == "unresolved"
    websocket = next(r for r in rows if r["path"] == "/socket")
    assert websocket["method"] == "WEBSOCKET" and websocket["status"] == "unsupported"


def test_discovery_failure_preserves_partial_snapshot_and_retry_history(tmp_path):
    document(tmp_path)
    (tmp_path / "bad.yaml").write_text("openapi: [invalid", encoding="utf-8")
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json", "bad.yaml"],
        )
    )
    assert (
        main(
            [
                "task",
                "--root",
                str(store.root),
                "discover",
                "demo",
                "--materials-root",
                str(tmp_path),
            ]
        )
        == 2
    )
    attempts = list((store.root / "demo/discovery").iterdir())
    assert len(attempts) == 1
    assert (attempts[0] / "failed.json").is_file()
    preserved = {p.name: p.read_bytes() for p in attempts[0].glob("*.json")}
    assert any("content" in json.loads(data) for data in preserved.values())
    assert store.load("demo").status == "created"
    document(tmp_path, "bad.yaml")
    inv = Inventory.model_validate_json(discover(store, "demo", tmp_path).read_text("utf-8"))
    assert inv.operations
    assert len(list((store.root / "demo/discovery").iterdir())) == 2
    assert {p.name: p.read_bytes() for p in attempts[0].glob("*.json")} == preserved


def test_interrupted_save_recovers_without_rescanning_or_overwriting(tmp_path, monkeypatch):
    document(tmp_path)
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json"],
        )
    )
    transition = store.transition

    def interrupt(*args):
        raise OSError("simulated interrupted metadata update")

    monkeypatch.setattr(store, "transition", interrupt)
    with pytest.raises(OSError):
        discover(store, "demo", tmp_path)
    saved = (store.root / "demo/inventory.json").read_bytes()
    (tmp_path / "api.json").write_text("changed after snapshot", encoding="utf-8")
    monkeypatch.setattr(store, "transition", transition)
    discover(store, "demo", tmp_path)
    assert (store.root / "demo/inventory.json").read_bytes() == saved
    assert store.load("demo").status == "discovered"


def test_local_openapi_refs_inherit_parameters_and_external_refs_stay_unresolved(tmp_path):
    data = {
        "openapi": "3.0.3",
        "components": {
            "parameters": {"Limit": {"name": "limit", "in": "query", "schema": {"type": "integer"}}}
        },
        "paths": {
            "/items": {
                "parameters": [{"$ref": "#/components/parameters/Limit"}],
                "get": {"responses": {"200": {"description": "OK"}}},
            },
            "/external": {"get": {"requestBody": {"$ref": "external.json#/body"}}},
        },
    }
    (tmp_path / "api.json").write_text(json.dumps(data), encoding="utf-8")
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json"],
        )
    )
    inv = Inventory.model_validate_json(discover(store, "demo", tmp_path).read_text("utf-8"))
    items = next(o for o in inv.operations if o.path == "/items")
    assert items.declarations[0]["parameters"][0]["name"] == "limit"
    assert next(o for o in inv.operations if o.path == "/external").status == "unresolved"


def test_parameter_auth_response_differences_are_retained():
    from api_test_agent.tasks.discovery import reconcile

    differences = reconcile(
        [
            {
                "details": {
                    "kind": "requirement",
                    "parameters": [{"name": "q", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {}},
                }
            },
            {
                "details": {
                    "kind": "implementation",
                    "parameters": [
                        {"name": "q", "required": False, "annotation": "int", "default": "None"}
                    ],
                    "options": {"status_code": "201"},
                    "authentication": "unresolved dependencies",
                }
            },
        ]
    )
    assert set(differences) == {
        "parameter_type:q:string!=integer",
        "parameter_required:q",
        "response_status_not_documented:201",
        "authentication_requires_confirmation",
    }


def test_duplicate_document_keys_and_inventory_tampering_rejected(tmp_path):
    from api_test_agent.tasks.discovery import openapi, ref

    content = 'openapi: "3.0.3"\npaths: {}\npaths: {}\n'
    with pytest.raises(ValueError, match="duplicate"):
        openapi(content, ref("api.yaml", content, "a" * 40))
    document(tmp_path)
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json"],
        )
    )
    path = discover(store, "demo", tmp_path)
    data = json.loads(path.read_text("utf-8"))
    data["snapshots"]["api.json"] += "\n"
    with pytest.raises(ValueError, match="hash"):
        Inventory.model_validate(data)


def test_identical_duplicate_declarations_are_not_a_conflict(tmp_path):
    """Bug 6a: identical method+path declarations (same signature) stay non-conflict."""
    document(tmp_path)
    # Same content, declared twice from two independent files.
    (tmp_path / "copy.json").write_text(
        (tmp_path / "api.json").read_text("utf-8"), encoding="utf-8"
    )
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["GET *", "POST *"],
            document_paths=["api.json", "copy.json"],
        )
    )
    inv = Inventory.model_validate_json(discover(store, "demo", tmp_path).read_text("utf-8"))
    by_key = {op.key: op for op in inv.operations}
    # Both duplicates collapse into one operation entry that keeps the source status.
    assert by_key["GET /v1/items"].status == "confirmed"
    assert len(by_key["GET /v1/items"].declarations) == 2
    assert "consistent declarations" in by_key["GET /v1/items"].reason
    # Divergent docs still conflict (existing behavior preserved).
    document(tmp_path, "other.json", "Changed description")
    store.create(
        Task(
            task_id="div",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["GET *"],
            document_paths=["api.json", "other.json"],
        )
    )
    div = Inventory.model_validate_json(discover(store, "div", tmp_path).read_text("utf-8"))
    assert div.operations[0].status == "conflict"


def test_explicit_openapi_adapter_parses_each_document_once(tmp_path):
    """Bug 6b: an explicitly selected openapi adapter must not cause double parsing."""
    document(tmp_path)
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["GET *"],
            document_paths=["api.json"],
            adapter="openapi",
        )
    )
    inv = Inventory.model_validate_json(discover(store, "demo", tmp_path).read_text("utf-8"))
    by_key = {op.key: op for op in inv.operations}
    assert by_key["GET /v1/items"].status == "confirmed"
    # Exactly one declaration/source: the adapter-offered parse is not duplicated.
    assert len(by_key["GET /v1/items"].declarations) == 1
    assert len(by_key["GET /v1/items"].sources) == 1
    assert inv.materials[0].path == "api.json"
    assert not any("openapi" in note and "duplicate" in note for note in inv.limitations)


def test_task_create_and_configure_cli_supports_repo_and_adapter(tmp_path):
    from api_test_agent.cli import main

    store_root = tmp_path / "runtime" / "tasks"
    rc = main(
        [
            "task",
            "--root",
            str(store_root),
            "create",
            "my-svc",
            "--project",
            "MyProject",
            "--commit",
            "b" * 40,
            "--repository",
            str(tmp_path / "repo"),
            "--source-paths",
            "src/",
            "app/",
            "--adapter",
            "python-ast",
        ]
    )
    assert rc == 0
    store = TaskStore(store_root)
    t = store.load("my-svc")
    assert t.target_project == "MyProject"
    assert t.target_commit == "b" * 40
    assert t.repository == str(tmp_path / "repo")
    assert t.source_paths == ("src/", "app/")
    assert t.adapter == "python-ast"

    rc2 = main(
        [
            "task",
            "--root",
            str(store_root),
            "configure",
            "my-svc",
            "--adapter",
            "openapi",
        ]
    )
    assert rc2 == 0
    t2 = store.load("my-svc")
    assert t2.adapter == "openapi"

    rc3 = main(
        [
            "task",
            "--root",
            str(store_root),
            "create",
            "my-doc-svc",
            "--project",
            "MyDocProject",
            "--commit",
            "0" * 40,
            "--openapi-url",
            "http://127.0.0.1:8080/v2/api-docs",
            "--adapter",
            "openapi",
        ]
    )
    assert rc3 == 0
    t3 = store.load("my-doc-svc")
    assert t3.openapi_url == "http://127.0.0.1:8080/v2/api-docs"
    assert t3.adapter == "openapi"
