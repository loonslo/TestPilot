"""Tests for TestPilot Web Console UI Server and API endpoints."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from api_test_agent.cli import _build_parser
from api_test_agent.tasks.store import TaskStore
from api_test_agent.ui.server import make_server


@pytest.fixture
def test_server(tmp_path: Path) -> Generator[tuple[str, TaskStore]]:
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    store = TaskStore(tasks_root)

    server = make_server(tasks_root, host="127.0.0.1", port=0)
    port = server.server_port
    base_url = f"http://127.0.0.1:{port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield base_url, store
    finally:
        server.shutdown()
        server.server_close()


def _request_json(
    url: str, method: str = "GET", payload: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"error": body}


def test_ui_serves_static_dashboard(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    req = urllib.request.Request(f"{base_url}/")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        content = resp.read().decode("utf-8")
        assert "<title>TestPilot" in content
        assert "Sources 追溯面板" in content


def test_ui_api_adapters(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    status, data = _request_json(f"{base_url}/api/adapters")
    assert status == 200
    assert "openapi" in data["adapters"]
    assert "fastapi" in data["adapters"]
    assert ".json" in data["extensions"]


def test_ui_api_models_env(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    status, data = _request_json(f"{base_url}/api/models/env")
    assert status == 200
    assert "DEEPSEEK_API_KEY" in data
    assert "API_TEST_AGENT_MODEL_API_KEY" in data
    assert isinstance(data["DEEPSEEK_API_KEY"], bool)
    assert isinstance(data["API_TEST_AGENT_MODEL_API_KEY"], bool)


def test_ui_task_lifecycle_api(test_server: tuple[str, TaskStore]) -> None:
    base_url, store = test_server

    # 1. Initial tasks list should be empty
    status, data = _request_json(f"{base_url}/api/tasks")
    assert status == 200
    assert data["tasks"] == []

    # 2. Create task via API
    create_payload = {
        "task_id": "task-test-01",
        "target_project": "TestProject",
        "target_commit": "a" * 40,
        "adapter": "openapi",
        "scope": ["/api/v1"],
    }
    status, created = _request_json(
        f"{base_url}/api/tasks/create", method="POST", payload=create_payload
    )
    assert status == 201
    assert created["task_id"] == "task-test-01"
    assert created["status"] == "created"

    # 3. List tasks should now return task-test-01
    status, data = _request_json(f"{base_url}/api/tasks")
    assert status == 200
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["task_id"] == "task-test-01"
    assert data["tasks"][0]["target_project"] == "TestProject"

    # 4. Get task detail
    status, detail = _request_json(f"{base_url}/api/tasks/task-test-01")
    assert status == 200
    assert detail["task_id"] == "task-test-01"

    # 5. Configure task
    config_payload = {
        "repository": "subjects/TestProject",
        "document_paths": ["openapi.json"],
    }
    status, updated = _request_json(
        f"{base_url}/api/tasks/task-test-01/configure",
        method="POST",
        payload=config_payload,
    )
    assert status == 200
    assert updated["repository"] == "subjects/TestProject"
    assert "openapi.json" in updated["document_paths"]

    # 6. Source inspection for task.json
    status, src = _request_json(f"{base_url}/api/tasks/task-test-01/source/task")
    assert status == 200
    assert src["badge"] == "task.json"
    assert "task-test-01" in src["content"]


def test_ui_model_ping(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server

    # Ping the local running server itself
    ping_payload = {
        "base_url": base_url,
        "api_key_env": "SOME_UNSET_KEY_VAR",
        "model": "deepseek-chat",
    }
    status, res = _request_json(f"{base_url}/api/model/ping", method="POST", payload=ping_payload)
    assert status == 200
    assert res["ok"] is True
    assert res["latency_ms"] >= 0
    assert res["key_configured"] is False


def test_ui_cli_parser_registered() -> None:
    parser = _build_parser()
    subparsers = [action for action in parser._actions if action.dest == "command"]
    assert subparsers, "Subparsers should exist"
    choices = subparsers[0].choices
    assert "ui" in choices
    ui_parser = choices["ui"]
    parsed = ui_parser.parse_args(["--port", "9090", "--no-open"])
    assert parsed.port == 9090
    assert parsed.no_open is True
