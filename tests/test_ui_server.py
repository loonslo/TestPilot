"""Tests for TestPilot Web Console UI Server, security guards, and API endpoints."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
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
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Content-Type": "application/json"} if payload is not None else {}
    if headers:
        req_headers.update(headers)
    if "X-TestPilot-Token" not in req_headers:
        # Read endpoints that expose host paths (e.g. /api/fs/list) require the
        # same session token as mutations, so every request carries it.
        parsed = urllib.parse.urlsplit(url)
        token_url = f"{parsed.scheme}://{parsed.netloc}/api/auth/token"
        try:
            with urllib.request.urlopen(token_url) as resp:
                t_data = json.loads(resp.read().decode("utf-8"))
                req_headers["X-TestPilot-Token"] = t_data.get("token", "")
        except Exception:
            pass
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
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
        assert "no-store" in resp.headers.get("Cache-Control", "")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        content = resp.read().decode("utf-8")
        assert "<title>TestPilot" in content
        assert "Sources 追溯面板" in content
        assert "testpilot-token" in content


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


def test_ui_post_without_token_rejected_403(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    # Explicitly pass empty token
    status, res = _request_json(
        f"{base_url}/api/tasks/create",
        method="POST",
        payload={"task_id": "bad-task"},
        headers={"X-TestPilot-Token": "invalid_wrong_token"},
    )
    assert status == 403
    assert "Forbidden" in res.get("error", "")


def test_ui_post_untrusted_origin_rejected_403(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    status, res = _request_json(
        f"{base_url}/api/tasks/create",
        method="POST",
        payload={"task_id": "evil-task"},
        headers={"Origin": "http://evil.com"},
    )
    assert status == 403
    assert "untrusted Origin" in res.get("error", "")


def test_ui_post_cross_port_origin_rejected_403(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    status, res = _request_json(
        f"{base_url}/api/tasks/create",
        method="POST",
        payload={"task_id": "port-task"},
        headers={"Origin": "http://127.0.0.1:9999"},
    )
    assert status == 403
    assert "cross-port Origin" in res.get("error", "")


def test_ui_post_cross_site_sec_fetch_site_rejected_403(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    status, res = _request_json(
        f"{base_url}/api/tasks/create",
        method="POST",
        payload={"task_id": "sec-task"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert "cross-site request blocked" in res.get("error", "")


def test_ui_fs_list_requires_session_token(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    status, res = _request_json(f"{base_url}/api/fs/list", headers={"X-TestPilot-Token": ""})
    assert status == 403
    assert "X-TestPilot-Token" in res.get("error", "")


def test_ui_fs_list_rejects_path_outside_allowed_roots(
    test_server: tuple[str, TaskStore], tmp_path: Path
) -> None:
    base_url, _ = test_server
    outside = urllib.parse.quote(str(tmp_path), safe="")
    status, res = _request_json(f"{base_url}/api/fs/list?path={outside}")
    assert status == 403
    assert "outside the console's allowed roots" in res.get("error", "")


def test_ui_fs_list_allows_extra_configured_root(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    extra_root = tmp_path / "subjects"
    extra_root.mkdir()
    (extra_root / "package.json").write_text("{}", encoding="utf-8")

    server = make_server(tasks_root, host="127.0.0.1", port=0, allowed_roots=(extra_root,))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        quoted = urllib.parse.quote(str(extra_root), safe="")
        status, data = _request_json(f"{base_url}/api/fs/list?path={quoted}")
        assert status == 200
        assert "package.json" in [item["name"] for item in data["items"]]
    finally:
        server.shutdown()
        server.server_close()


def test_ui_configure_accepts_openapi_url(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    status, _created = _request_json(
        f"{base_url}/api/tasks/create",
        method="POST",
        payload={
            "task_id": "url-task-01",
            "target_project": "UrlProject",
            "target_commit": "b" * 40,
            "adapter": "openapi",
            "scope": ["*"],
        },
    )
    assert status == 201

    status, updated = _request_json(
        f"{base_url}/api/tasks/url-task-01/configure",
        method="POST",
        payload={"openapi_url": "https://example.com/openapi.json"},
    )
    assert status == 200
    assert updated["openapi_url"] == "https://example.com/openapi.json"


def test_ui_cli_parser_registered() -> None:
    parser = _build_parser()
    subparsers = [action for action in parser._actions if action.dest == "command"]
    assert subparsers, "Subparsers should exist"
    choices = subparsers[0].choices
    assert "ui" in choices
    ui_parser = choices["ui"]
    parsed = ui_parser.parse_args(["--port", "9090", "--no-open", "--allow-root", "subjects"])
    assert parsed.port == 9090
    assert parsed.no_open is True
    assert parsed.allow_root == [Path("subjects")]


def test_ui_task_create_with_openapi_and_discover(test_server: tuple[str, TaskStore]) -> None:
    base_url, store = test_server
    create_payload = {
        "task_id": "doc-task-01",
        "target_project": "DocProject",
        "target_commit": "0000000000000000000000000000000000000000",
        "adapter": "openapi",
        "document_paths": ["examples/tasks/openapi-demo.json"],
        "scope": ["*"],
    }
    status, created = _request_json(
        f"{base_url}/api/tasks/create", method="POST", payload=create_payload
    )
    assert status == 201
    assert created["task_id"] == "doc-task-01"

    status, inv = _request_json(f"{base_url}/api/tasks/doc-task-01/discover", method="POST")
    assert status == 200
    assert "operations" in inv
    assert len(inv["operations"]) > 0

    # The transition history carries datetimes; the detail endpoint must
    # serialize them rather than dropping the connection.
    status, detail = _request_json(f"{base_url}/api/tasks/doc-task-01")
    assert status == 200
    assert detail["status"] == "discovered"
    assert detail["history"], "discovery records a transition"
    assert isinstance(detail["history"][0]["at"], str)


def test_ui_fs_list_api(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    # 1. Default list (workspace root)
    status, data = _request_json(f"{base_url}/api/fs/list")
    assert status == 200
    assert "current_path" in data
    assert "items" in data
    assert "quick_dirs" in data
    assert len(data["items"]) > 0

    # 2. Filter directories
    status, dir_data = _request_json(f"{base_url}/api/fs/list?filter=dir")
    assert status == 200
    assert all(item["is_dir"] for item in dir_data["items"])

    # 3. Specific path (examples/tasks)
    status, task_data = _request_json(f"{base_url}/api/fs/list?path=examples/tasks&filter=doc")
    assert status == 200
    names = [item["name"] for item in task_data["items"]]
    assert "openapi-demo.json" in names


def test_ui_dashboard_styling_and_explorer_elements(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    req = urllib.request.Request(f"{base_url}/")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        html = resp.read().decode("utf-8")
        # 1. Ledger palette: graphite ground, amber structural accent, emerald status
        assert "--bg: #09090b" in html
        assert "--surface: #121215" in html
        assert "--primary: #10b981" in html
        assert "--accent: #d8a24a" in html
        assert "#0284c7" not in html
        assert "#38bdf8" not in html
        assert "#090d16" not in html

        # 2. Hand-written CSS only: no third-party CDN script under the strict CSP
        assert "gstatic.com" not in html
        assert "tailwind" not in html.lower()

        # 3. No fabricated pipeline data (the dashboard reads real endpoints)
        assert "NullPointerException" not in html
        assert "approveAll" not in html
        assert "RecruitService" not in html

        # 4. Filesystem Explorer Modal and buttons exist
        assert "modalFileBrowser" in html
        assert "openFileBrowser" in html
        assert "browserItemsList" in html
        assert "browserQuickDirs" in html

        # 5. Sharp corners (no rounded-2xl utility classes)
        assert "rounded-2xl" not in html

        # 6. Folder multi-selection support
        assert "chkMultiSelect" in html
        assert "selectAllFolders" in html
        assert "browserSelectedChipsBar" in html
        assert "多选" in html

        # 7. The light theme is its own warm-paper palette (not inverted graphite),
        #    and the header carries a labelled switcher for both themes.
        assert "html.light {" in html
        assert "--bg: #e9e6dd" in html
        assert "--surface: #fbf9f4" in html
        assert 'id="themeDark"' in html
        assert 'id="themeLight"' in html
        assert "深色" in html and "浅色" in html
        assert "prefers-color-scheme: light" in html
        # The saved choice must be applied before first paint, not after boot.
        assert html.index("testpilot-theme") < html.index("<style>")

        # 8. One hairline radius token on controls and cards (not a pill), and
        #    small buttons sized to the taller control they sit beside.
        assert "--radius: 3px" in html
        assert "border-radius: var(--radius)" in html
        assert ".with-btn .btn-sm { height: 32px" in html
        assert ".browser-tools .btn-sm { height: 28px; }" in html

        # 9. The Sources bar shares the header row with the sheet, so their
        #    rules line up; it must stay a sibling of the drawer, not inside it.
        assert 'id="drawerHead"' in html
        assert html.index('id="drawerHead"') < html.index('<aside class="drawer"')
        assert "grid-template-rows: auto minmax(0, 1fr)" in html
        assert ".app.no-drawer" in html
