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
