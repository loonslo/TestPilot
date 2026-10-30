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
