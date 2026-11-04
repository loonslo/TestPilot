"""Tests for the pluggable adapter framework (Python AST, OpenAPI, and runner registry)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from api_test_agent.adapters import (
    DiscoveryAdapter,
    OpenAPIAdapter,
    PythonAstAdapter,
    get_adapter,
    get_all_supported_extensions,
    list_adapters,
    register_adapter,
)
from api_test_agent.adapters.python_ast import literal
from api_test_agent.regression.runner import (
    RUNNER_REGISTRY,
    get_runner,
    pytest_command,
    register_runner,
)
from api_test_agent.tasks.discovery import permitted
from api_test_agent.tasks.models import (
    CaseRevision,
    SourceRef,
    Task,
)


def test_builtin_adapters_registered():
    assert isinstance(get_adapter("python-ast"), DiscoveryAdapter)
    assert isinstance(get_adapter("python_ast"), DiscoveryAdapter)
    assert isinstance(get_adapter("fastapi"), DiscoveryAdapter)
    assert isinstance(get_adapter("openapi"), DiscoveryAdapter)
    assert isinstance(get_adapter("swagger"), DiscoveryAdapter)
    assert "python-ast" in list_adapters()
    assert "openapi" in list_adapters()


def test_custom_adapter_registration_and_permitted_extensions():
    class JavaSpringAdapter:
        name = "java-spring"
        supported_extensions = (".java",)

        def extract(
            self, snapshots: dict[str, str], sources: dict[str, SourceRef]
        ) -> tuple[list[dict], list[str]]:
            return [], []

    adapter = JavaSpringAdapter()
    assert isinstance(adapter, DiscoveryAdapter)
    register_adapter(adapter, "spring", "java")

    assert get_adapter("java-spring") is adapter
    assert get_adapter("spring") is adapter
    assert ".java" in get_all_supported_extensions()
    assert permitted("src/main/java/com/apple/recruit/RecruitController.java")
    # Denied or hidden files are still excluded even with allowed extensions
    assert not permitted("src/test/RecruitTest.java")
    assert not permitted("data/RecruitController.java")
    assert not permitted(".hidden/RecruitController.java")


def test_python_ast_adapter_extraction():
    adapter = PythonAstAdapter()
    source_content = """from fastapi import FastAPI, APIRouter
app = FastAPI()
router = APIRouter(prefix="/jobs")

@router.get("/list")
def list_jobs():
    pass

app.include_router(router, prefix="/v1")
"""
    source = SourceRef(
        source_id="src01",
        version="a" * 40,
        path="app.py",
        locator="full file",
        content_hash="0" * 64,
        summary="code",
    )
    rows, warnings = adapter.extract({"app.py": source_content}, {"app.py": source})
    assert not warnings
    assert len(rows) == 1
    assert rows[0]["method"] == "GET"
    assert rows[0]["path"] == "/v1/jobs/list"
    assert rows[0]["status"] == "confirmed"


def test_python_ast_literal_robust_exception_handling():
    import ast

    # Normal literal evaluation
    node = ast.parse("123", mode="eval").body
    assert literal(node) == 123

    # Dynamic expression raises ValueError
    dyn_node = ast.parse("func_call()", mode="eval").body
    with pytest.raises(ValueError, match="dynamic expression"):
        literal(dyn_node)


def test_python_ast_adapter_expands_in_set_pydantic_request_model():
    adapter = PythonAstAdapter()
    source_content = """from fastapi import FastAPI, APIRouter
from pydantic import BaseModel

app = FastAPI()
router = APIRouter(prefix="/jobs")

class JobRequest(BaseModel):
    title: str
    owner_id: int = 0
    active: bool = True

@router.post("/create")
def create_job(payload: JobRequest):
    pass

app.include_router(router, prefix="/v1")
"""
    source = SourceRef(
        source_id="src01",
        version="a" * 40,
        path="app.py",
        locator="full file",
        content_hash="0" * 64,
        summary="code",
    )
    rows, warnings = adapter.extract({"app.py": source_content}, {"app.py": source})
    assert not warnings
    assert len(rows) == 1
    row = rows[0]
    assert row["method"] == "POST"
    assert row["path"] == "/v1/jobs/create"
    bodies = row["details"]["request_body"]
    assert len(bodies) == 1
    body = bodies[0]
    assert body["kind"] == "request_body"
    assert body["model"] == "app.JobRequest"
    fields = {f["name"]: f for f in body["fields"]}
    assert set(fields) == {"title", "owner_id", "active"}
    assert fields["title"]["required"] is True
    assert fields["owner_id"]["required"] is False
    assert fields["owner_id"]["default"] == "0"
    assert fields["active"]["type"].endswith("bool")


def test_python_ast_adapter_does_not_expand_out_of_set_model():
    adapter = PythonAstAdapter()
    # JobRequest is referenced but defined in another module, so it is not expanded.
    source_content = """from fastapi import FastAPI
from schemas import JobRequest

app = FastAPI()

@app.post("/create")
def create_job(payload: JobRequest):
    pass
"""
    source = SourceRef(
        source_id="src01",
        version="a" * 40,
        path="app.py",
        locator="full file",
        content_hash="0" * 64,
        summary="code",
    )
    rows, _ = adapter.extract({"app.py": source_content}, {"app.py": source})
    assert len(rows) == 1
    bodies = rows[0]["details"]["request_body"]
    assert bodies == []


def test_openapi_adapter_v3_extraction():
    adapter = OpenAPIAdapter()
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": "http://api.example.com/api"}],
        "paths": {
            "/candidates": {
                "post": {
                    "operationId": "createCandidate",
                    "responses": {"201": {"description": "Created"}},
                }
            }
        },
    }
    content = json.dumps(spec)
    source = SourceRef(
        source_id="spec01",
        version="a" * 40,
        path="openapi.json",
        locator="full file",
        content_hash="0" * 64,
        summary="spec",
    )
    rows, warnings = adapter.extract({"openapi.json": content}, {"openapi.json": source})
    assert len(rows) == 1
    assert rows[0]["method"] == "POST"
    assert rows[0]["path"] == "/api/candidates"
    assert rows[0]["original_operation_ids"] == ["createCandidate"]


def test_openapi_operation_and_path_server_override_root_prefix():
    adapter = OpenAPIAdapter()
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": "http://api.example.com/root"}],
        "paths": {
            "/a": {
                "get": {
                    "operationId": "opA",
                    "responses": {"200": {"description": "OK"}},
                    "servers": [{"url": "http://api.example.com/op"}],
                }
            },
            "/b": {
                "servers": [{"url": "http://api.example.com/path"}],
                "get": {
                    "operationId": "opB",
                    "responses": {"200": {"description": "OK"}},
                },
            },
        },
    }
    source = SourceRef(
        source_id="spec01",
        version="a" * 40,
        path="openapi.json",
        locator="full file",
        content_hash="0" * 64,
        summary="spec",
    )
    rows, _ = adapter.extract({"openapi.json": json.dumps(spec)}, {"openapi.json": source})
    paths = {r["original_operation_ids"][0]: r["path"] for r in rows}
    assert paths["opA"] == "/op/a"  # operation-level overrides both root and path
    assert paths["opB"] == "/path/b"  # path-level overrides root


def test_openapi_operation_parameter_overrides_path_level():
    adapter = OpenAPIAdapter()
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": "http://api.example.com"}],
        "paths": {
            "/items/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
                ],
                "get": {
                    "operationId": "getItem",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        },
                        {"name": "verbose", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            }
        },
    }
    source = SourceRef(
        source_id="spec01",
        version="a" * 40,
        path="openapi.json",
        locator="full file",
        content_hash="0" * 64,
        summary="spec",
    )
    rows, _ = adapter.extract({"openapi.json": json.dumps(spec)}, {"openapi.json": source})
    params = rows[0]["details"]["parameters"]
    by_name = {p["name"]: p for p in params if isinstance(p, dict)}
    # Operation-level "id" wins over path-level "id".
    assert by_name["id"]["schema"]["type"] == "integer"
    assert by_name["verbose"]["schema"]["type"] == "string"
    assert len([p for p in params if isinstance(p, dict) and p["name"] == "id"]) == 1


def test_openapi_external_ref_warns_and_stays_unresolved():
    adapter = OpenAPIAdapter()
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": "http://api.example.com"}],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "requestBody": {"$ref": "external.json#/body"},
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    source = SourceRef(
        source_id="spec01",
        version="a" * 40,
        path="openapi.json",
        locator="full file",
        content_hash="0" * 64,
        summary="spec",
    )
    rows, warnings = adapter.extract({"openapi.json": json.dumps(spec)}, {"openapi.json": source})
    assert rows[0]["status"] == "unresolved"
    assert any("external/cross-file" in w and "external.json" in w for w in warnings)


def test_openapi_cross_file_nested_refs_and_provenance():
    from api_test_agent.tasks.discovery import ref

    snapshots = {
        "api/main.json": json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {"/items": {"post": {"requestBody": {"$ref": "parts/body.json#/body"}}}},
            }
        ),
        "api/parts/body.json": json.dumps(
            {
                "body": {
                    "content": {"application/json": {"schema": {"$ref": "../models.json#/Item"}}}
                }
            }
        ),
        "api/models.json": json.dumps(
            {
                "Item": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "child": {"$ref": "#/Item"}},
                }
            }
        ),
    }
    sources = {p: ref(p, c, "a" * 40) for p, c in snapshots.items()}
    rows, _ = OpenAPIAdapter().extract(snapshots, sources)
    assert len(rows) == 1
    schema = rows[0]["details"]["request_body"]["content"]["application/json"]["schema"]
    assert schema["properties"]["name"]["type"] == "string"
    assert "$ref" in schema["properties"]["child"]
    assert rows[0]["status"] == "unresolved"
    assert {s.path for s in rows[0]["sources"]} == set(snapshots)


def test_openapi_server_overrides_do_not_leak_and_allow_empty_prefix():
    from api_test_agent.tasks.discovery import ref

    spec = {
        "openapi": "3.0.3",
        "servers": [{"url": "/root"}],
        "paths": {
            "/a": {"servers": [{"url": "/path"}], "get": {"servers": [{"url": "/op"}]}, "post": {}},
            "/b": {"get": {}},
            "/c": {"servers": [{"url": "https://example.invalid"}], "get": {}},
            "/d": {"get": {"servers": []}},
        },
    }
    content = json.dumps(spec)
    rows, _ = OpenAPIAdapter().extract(
        {"api.json": content}, {"api.json": ref("api.json", content, "a" * 40)}
    )
    assert {(r["method"], r["path"]) for r in rows} == {
        ("GET", "/op/a"),
        ("POST", "/path/a"),
        ("GET", "/root/b"),
        ("GET", "/c"),
        ("GET", "/d"),
    }


def test_openapi_adapter_swagger_v2_extraction():
    adapter = OpenAPIAdapter()
    spec = {
        "swagger": "2.0",
        "basePath": "/v2",
        "paths": {
            "/applications": {
                "get": {
                    "operationId": "getApplications",
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    content = json.dumps(spec)
    source = SourceRef(
        source_id="spec02",
        version="a" * 40,
        path="swagger.json",
        locator="full file",
        content_hash="0" * 64,
        summary="swagger spec",
    )
    rows, warnings = adapter.extract({"swagger.json": content}, {"swagger.json": source})
    assert len(rows) == 1
    assert rows[0]["method"] == "GET"
    assert rows[0]["path"] == "/v2/applications"
    assert rows[0]["original_operation_ids"] == ["getApplications"]


def test_openapi_adapter_handles_invalid_and_non_openapi():
    adapter = OpenAPIAdapter()
    source = SourceRef(
        source_id="spec03",
        version="a" * 40,
        path="readme.json",
        locator="full file",
        content_hash="0" * 64,
        summary="non openapi",
    )
    # Non-openapi json
    rows, warnings = adapter.extract({"readme.json": '{"title": "hello"}'}, {"readme.json": source})
    assert rows == []
    assert any("no OpenAPI document" in w for w in warnings)

    # Malformed json/yaml raises
    bad_source = SourceRef(
        source_id="spec04",
        version="a" * 40,
        path="broken.json",
        locator="full file",
        content_hash="0" * 64,
        summary="broken",
    )
    import yaml

    with pytest.raises(yaml.YAMLError):
        adapter.extract({"broken.json": "{"}, {"broken.json": bad_source})


def test_task_model_adapter_and_version_manifest():
    # Single target_commit
    task1 = Task(
        task_id="task-commit",
        target_project="project-a",
        target_commit="a" * 40,
        scope=["*"],
        document_paths=["spec.json"],
        adapter="openapi",
    )
    task1.require_target()
    assert task1.adapter == "openapi"

    # Multi-repo version_manifest
    task2 = Task(
        task_id="task-manifest",
        target_project="apple-system",
        version_manifest={
            "APPLE-recruitService": "1" * 40,
            "APPLE-authService": "2" * 40,
        },
        scope=["*"],
        document_paths=["spec.json"],
    )
    task2.require_target()
    assert task2.version_manifest["APPLE-recruitService"] == "1" * 40


def test_case_revision_allows_pluggable_execution_kinds():
    base_case = {
        "case_id": "case-one",
        "revision": 1,
        "title": "Synthetic example",
        "origin": "human",
        "target_commit": "a" * 40,
        "operation_ids": ["list"],
        "purpose": "Check response",
        "scenario": "normal",
        "rules": [
            {
                "rule_id": "r1",
                "statement": "Return list",
                "basis": "requirement",
                "sources": [
                    {
                        "source_id": "s1",
                        "version": "a" * 40,
                        "path": "docs/spec.md",
                        "locator": "section 1",
                        "content_hash": "b" * 64,
                        "summary": "Synthetic specification",
                    }
                ],
            }
        ],
        "steps": [
            {
                "step_id": "request",
                "phase": "test",
                "operation_id": "list",
                "account": "synthetic",
                "side_effect": False,
                "parameters": [
                    {
                        "name": "limit",
                        "location": "query",
                        "mode": "literal",
                        "value": 1,
                        "role": "background",
                        "reason": "Valid baseline",
                        "source_ids": ["s1"],
                    }
                ],
            }
        ],
        "assertions": [{"expression": "status == 200", "expected": 200, "rule_ids": ["r1"]}],
        "change_summary": "Initial design",
    }

    # Default http
    case_http = CaseRevision(**base_case)
    assert case_http.execution_kind == "http"

    # Pluggable execution kind (junit, mvn, jest)
    case_junit = CaseRevision(
        **{**deepcopy(base_case), "case_id": "case-junit", "execution_kind": "junit"}
    )
    assert case_junit.execution_kind == "junit"

    # Pytest snapshot adapter still rejected
    with pytest.raises(ValueError, match="pytest snapshot adapter is not implemented"):
        CaseRevision(
            **{**deepcopy(base_case), "case_id": "case-pytest", "execution_kind": "pytest"}
        )


def test_runner_registry_pluggability():
    assert "pytest" in RUNNER_REGISTRY
    assert get_runner("pytest") is pytest_command

    def custom_mvn_builder(python, entry, repo, extra_args=None):
        return ["mvn", "test", f"-Dtest={entry[0]}"]

    register_runner("mvn", custom_mvn_builder)
    assert get_runner("mvn") is custom_mvn_builder
    cmd = get_runner("mvn")(None, ["RecruitServiceTest"], Path("."), None)
    assert cmd == ["mvn", "test", "-Dtest=RecruitServiceTest"]

    with pytest.raises(ValueError, match="unknown test runner: non-existent"):
        get_runner("non-existent")
