"""Tests for TestPilot Web Console UI Server, security guards, and API endpoints."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_task_design import prepared as prepared
from test_task_execution import approved_setup as approved_setup

from api_test_agent.cli import _build_parser
from api_test_agent.tasks.discovery import Inventory, model_materials
from api_test_agent.tasks.store import TaskStore
from api_test_agent.ui.server import make_server


@pytest.fixture
def test_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[str, TaskStore]]:
    monkeypatch.setattr("api_test_agent.ui.server.read_env", lambda _: None)
    monkeypatch.setattr("api_test_agent.ui.workflow.read_key", lambda _: None)
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
        assert "技术详情" in content
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


def test_web_model_key_is_write_only_scoped_and_removable(test_server, monkeypatch):
    base_url, store = test_server
    vault = {}

    def write(target, key):
        if key is None:
            vault.pop(target, None)
        else:
            vault[target] = key

    monkeypatch.setattr("api_test_agent.ui.workflow.read_key", vault.get)
    monkeypatch.setattr("api_test_agent.ui.workflow.write_key", write)
    monkeypatch.setattr("api_test_agent.ui.workflow.read_env", lambda _: None)
    url = f"{base_url}/api/model/config"
    first = {"base_url": "https://first.example.invalid/v1", "model_id": "model-a"}
    second = {"base_url": "https://other.example.invalid/v1", "model_id": "model-a"}
    key = "synthetic-key-for-web-configuration"
    status, configured = _request_json(url, "POST", first | {"api_key": key})
    assert status == 200 and configured["stored_key_configured"] is True
    assert tuple(vault.values()) == (key,)
    assert key not in json.dumps(configured) and key not in json.dumps(_request_json(url)[1])
    assert key not in (store.root / ".web-model.json").read_text("utf-8")
    assert _request_json(url, "POST", second)[1]["key_configured"] is False
    assert _request_json(url, "POST", first)[1]["stored_key_configured"] is True
    status, error = _request_json(
        url,
        "POST",
        second
        | {
            "base_url": "http://other.example.invalid/v1",
            "api_key": key,
        },
    )
    assert status == 400 and key not in json.dumps(error)
    assert (
        _request_json(url, "POST", first | {"remove_api_key": True})[1]["key_configured"] is False
    )
    assert not vault


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
    assert "untrusted Host or Origin" in res.get("error", "")


def test_ui_post_cross_port_origin_rejected_403(test_server: tuple[str, TaskStore]) -> None:
    base_url, _ = test_server
    status, res = _request_json(
        f"{base_url}/api/tasks/create",
        method="POST",
        payload={"task_id": "port-task"},
        headers={"Origin": "http://127.0.0.1:9999"},
    )
    assert status == 403
    assert "untrusted Host or Origin" in res.get("error", "")


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


def test_ui_fs_lists_external_directory_and_filters_protected_paths(
    test_server: tuple[str, TaskStore], tmp_path: Path
) -> None:
    base_url, _ = test_server
    outside = tmp_path / "other-project"
    outside.mkdir()
    (outside / "api.json").write_text("{}", encoding="utf-8")
    (outside / "credentials.json").write_text("{}", encoding="utf-8")
    (outside / ".git").mkdir()
    quoted = urllib.parse.quote(str(outside), safe="")
    status, result = _request_json(f"{base_url}/api/fs/list?path={quoted}&filter=doc")
    assert status == 200
    assert [item["name"] for item in result["items"]] == ["api.json"]
    assert any(Path(item["path"]) == Path(outside.anchor) for item in result["quick_dirs"])
    forbidden = urllib.parse.quote(str(outside / ".git"), safe="")
    status, _ = _request_json(f"{base_url}/api/fs/list?path={forbidden}")
    assert status == 403
    status, _ = _request_json(
        f"{base_url}/api/tasks/create",
        "POST",
        {"document_paths": [str(outside / "credentials.json")]},
    )
    assert status == 403


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


def test_ui_source_directory_without_git(test_server, tmp_path, monkeypatch):
    def unexpected_git(*args):
        pytest.fail("ordinary local browsing and discovery must not call Git")

    monkeypatch.setattr("api_test_agent.ui.server.git", unexpected_git)
    monkeypatch.setattr("api_test_agent.tasks.discovery.git", unexpected_git)
    base_url, store = test_server
    root = tmp_path / "delivery"
    root.mkdir()
    (root / "main.py").write_text(
        'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/delivery")\ndef read(): pass\n',
        "utf-8",
    )
    (root / "openapi.json").write_text(
        Path("examples/tasks/openapi-demo.json").read_text("utf-8"), "utf-8"
    )
    params = urllib.parse.urlencode({"path": root})
    status, listing = _request_json(f"{base_url}/api/fs/list?{params}")
    assert status == 200 and "repo_root" not in listing
    status, _ = _request_json(
        f"{base_url}/api/tasks/create",
        "POST",
        {
            "task_id": "directory-task",
            "target_project": "Delivery",
            "scope": ["*"],
        },
    )
    assert status == 201
    task_url = f"{base_url}/api/tasks/directory-task"
    status, configured = _request_json(
        f"{task_url}/configure",
        "POST",
        {
            "source_directory": str(root),
            "adapter": "python-ast",
            "detect_repository": True,
        },
    )
    assert status == 200
    assert configured["repository"] is None and configured["target_commit"] is None
    status, inventory = _request_json(f"{task_url}/discover", "POST")
    assert status == 200
    assert {operation["path"] for operation in inventory["operations"]} == {"/delivery", "/items"}
    assert store.load("directory-task").source_directory == str(root)


def test_ui_auto_uses_exact_git_root_and_freezes_committed_inputs(test_server, tmp_path):
    base_url, _ = test_server
    repo = tmp_path / "repository"
    service = repo / "service"
    service.mkdir(parents=True)

    def run_git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    source = service / "main.py"
    python = 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/{}")\ndef read(): pass\n'
    source.write_text(python.format("committed"), "utf-8")
    (service / "JobsController.java").write_text(
        "import org.springframework.web.bind.annotation.*;\n"
        '@RestController @RequestMapping("/java") class JobsController {\n'
        '  @GetMapping("/jobs") void jobs() {}\n}\n',
        "utf-8",
    )
    (repo / "vendor").mkdir()
    (repo / "vendor" / "library.py").write_text(python.format("vendor-route"), "utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "api.yaml").write_text(
        Path("examples/tasks/openapi-demo.json").read_text("utf-8"), "utf-8"
    )
    (repo / "backend").mkdir()
    (repo / "backend" / "UsersController.java").write_text("class UsersController {}", "utf-8")
    run_git("init")
    run_git("add", ".")
    run_git(
        "-c", "user.name=Fixture", "-c", "user.email=qa@example.invalid", "commit", "-m", "Fixture"
    )
    commit = run_git("rev-parse", "HEAD")
    source.write_text(python.format("working-copy"), "utf-8")

    def discover_directory(name, directory):
        status, task = _request_json(
            f"{base_url}/api/tasks/create",
            "POST",
            {
                "task_id": name,
                "target_project": "Delivery",
                "scope": ["*"],
                "source_directory": str(directory),
                "detect_repository": True,
                "adapter": "python-ast",
            },
        )
        assert status == 201, task
        status, result = _request_json(f"{base_url}/api/tasks/{name}/discover", "POST")
        assert status == 200, result
        return task, Inventory.model_validate(result)

    task, inventory = discover_directory("repo-source", repo)
    assert task["repository"] == str(repo) and task["target_commit"] == commit
    assert task["source_paths"] == ["."] and inventory.version_verified
    assert {op.path for op in inventory.operations} == {"/committed", "/items", "/java/jobs"}
    assert json.loads(model_materials(inventory, 40000))["materials"] == []

    task, inventory = discover_directory("ordinary-subfolder", service)
    assert task["repository"] is None and task["target_commit"] is None
    assert {op.path for op in inventory.operations} == {"/working-copy", "/java/jobs"}

    task_url = f"{base_url}/api/tasks/java-only"
    status, _ = _request_json(
        f"{base_url}/api/tasks/create",
        "POST",
        {
            "task_id": "java-only",
            "target_project": "Delivery",
            "scope": ["*"],
            "source_directory": str(repo),
            "source_paths": ["backend"],
            "detect_repository": True,
            "adapter": "python-ast",
        },
    )
    assert status == 201
    status, error = _request_json(f"{task_url}/discover", "POST")
    assert status == 400
    assert "检测到 Java 源码" in error["error"] and "填写在线规格地址" in error["error"]
    assert _request_json(task_url)[1]["status"] == "created"
    status, _ = _request_json(
        f"{task_url}/configure",
        "POST",
        {
            "repository": None,
            "source_paths": [],
            "target_commit": None,
            "document_paths": [str(repo / "docs" / "api.yaml")],
            "adapter": "openapi",
        },
    )
    assert status == 200
    status, result = _request_json(f"{task_url}/discover", "POST")
    assert status == 200 and {operation["path"] for operation in result["operations"]} == {"/items"}


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
        assert html.index('id="drawerHead"') < html.index('<aside class="drawer')
        assert 'class="app no-drawer"' in html
        assert "grid-template-rows: auto minmax(0, 1fr)" in html
        assert ".app.no-drawer" in html


def test_web_workflow_runs_scoped_batches_and_preserves_old_report(
    test_server, prepared, tmp_path, monkeypatch
):
    base_url, _ = test_server
    _, _, template = prepared
    request_started = threading.Event()
    release_request = threading.Event()
    calls = []

    class SyntheticService(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            request_started.set()
            release_request.wait(timeout=5)
            body = b'{"items": [1]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    target = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticService)
    thread = threading.Thread(target=target.serve_forever, daemon=True)
    thread.start()

    def get(path, expected=200):
        status, data = _request_json(f"{base_url}{path}")
        assert status == expected, data
        return data

    def post(path, data=None, expected=200):
        status, result = _request_json(f"{base_url}{path}", "POST", data or {})
        assert status == expected, result
        return result

    def wait_job(task_path, job):
        for _ in range(150):
            jobs = get(f"{task_path}/workflow")["jobs"]
            state = next(item for item in jobs if item["job_id"] == job["job_id"])
            if state["status"] != "running":
                return state
            time.sleep(0.025)
        pytest.fail("Background job did not finish")

    def human_approval(task_path, ids):
        review = post(f"{task_path}/review")
        assert all(case_id in review["markdown"] for case_id in ids)
        payload = {
            "review_id": review["review_id"],
            "review_hash": review["review_hash"],
            "actor": "synthetic-reviewer",
            "decisions": [
                {
                    "case_id": case_id,
                    "revision_hash": review["cases"][case_id],
                    "decision": "approved",
                    "reason": "Checked parameters and expected status",
                }
                for case_id in ids
            ],
        }
        return post(f"{task_path}/approve", payload)["approval_id"], payload

    try:
        task = post(
            "/api/tasks/create",
            {
                "target_project": "Synthetic UI workflow",
                "scope": ["*"],
                "document_paths": [str(tmp_path / "api.json")],
            },
            201,
        )
        assert task["task_id"].startswith("task-")
        assert task["target_commit"] is None
        task_path = f"/api/tasks/{task['task_id']}"
        inventory = post(f"{task_path}/discover")
        operation_id = inventory["operations"][0]["operation_id"]
        source = inventory["materials"][0]
        case = deepcopy(template["cases"][0])
        case.update(target_commit=None, operation_ids=[operation_id])
        case["rules"][0]["sources"] = [source]
        case["steps"][0]["operation_id"] = operation_id
        case["steps"][0]["parameters"][0]["source_ids"] = [source["source_id"]]
        assessments = [{**row, "operation_id": operation_id} for row in template["assessments"]]
        design_output = {"cases": [case], "assessments": assessments}

        provider_key = "synthetic-key-for-generation"
        vault = {}

        def fake_generator(settings, *, api_key):
            assert settings.model_id == "unknown-model"
            assert api_key == provider_key

            def generate(request):
                assert provider_key not in request
                assert json.loads(request)["operations"][0]["operation_id"] == operation_id
                return json.dumps(design_output)

            return generate

        monkeypatch.setattr("api_test_agent.ui.workflow.model_generator", fake_generator)
        monkeypatch.setattr("api_test_agent.ui.workflow.read_env", lambda _: None)
        monkeypatch.setattr("api_test_agent.ui.workflow.read_key", vault.get)
        monkeypatch.setattr("api_test_agent.ui.workflow.write_key", vault.__setitem__)
        config = post(
            "/api/model/config",
            {
                "model_id": "unknown-model",
                "base_url": "https://example.invalid/v1",
                "api_key_env": "SYNTHETIC_PROVIDER_KEY",
                "api_key": provider_key,
            },
        )
        assert config["key_configured"] is True
        assert provider_key not in json.dumps(get("/api/model/config"))
        generated = post(f"{task_path}/generate", {"operation_ids": [operation_id]}, 202)
        assert wait_job(task_path, generated)["status"] == "completed"
        extra_case = {**case, "case_id": "additional-list-check"}
        post(
            f"{task_path}/cases",
            {"response": json.dumps({**design_output, "cases": [extra_case]})},
        )
        post(
            f"{task_path}/environment",
            {
                "environment": "local",
                "base_url": f"http://127.0.0.1:{target.server_port}",
                "allowed_operations": [operation_id],
            },
        )
        post(f"{task_path}/execute", {"approval_id": "unapproved"}, 403)
        assert not calls

        approval_id, original_review = human_approval(
            task_path, [case["case_id"], extra_case["case_id"]]
        )
        first = post(f"{task_path}/execute", {"approval_id": approval_id}, 202)
        assert request_started.wait(timeout=5)
        assert get(f"{task_path}/batches")["batches"][0]["status"] == "running"
        post(f"{task_path}/execute", {"approval_id": approval_id}, 409)
        release_request.set()
        assert wait_job(task_path, first)["outcome"] == "passed"
        first_batch = f"{task_path}/batches/{first['execution_id']}"
        first_result = get(first_batch)
        assert first_result["report"]["accounting"]["passed"] == 2
        evidence_id = first_result["records"][0]["evidence_ids"][0]
        evidence = get(f"{first_batch}/evidence/{evidence_id}")
        assert evidence["evidence_id"] == evidence_id

        scoped = post(
            f"{task_path}/execute",
            {
                "approval_id": approval_id,
                "case_ids": [case["case_id"]],
                "previous_execution_id": first["execution_id"],
            },
            202,
        )
        assert wait_job(task_path, scoped)["outcome"] == "passed"
        scoped_result = get(f"{task_path}/batches/{scoped['execution_id']}")
        assert scoped_result["batch"]["previous_execution_id"] == first["execution_id"]
        assert scoped_result["report"]["accounting"]["planned"] == 1

        edited = deepcopy(case)
        edited["assertions"][0]["expected"] = False
        edited["change_summary"] = "Intentionally verify mismatch is reported"
        current = get(f"{task_path}/workflow")["case_hashes"]
        revised = post(
            f"{task_path}/cases/{case['case_id']}",
            {"expected_hash": current[case["case_id"]], "case": edited},
        )
        assert revised["revision"] == 2
        assert get(f"{task_path}/workflow")["approval"]["stale"] is True
        post(f"{task_path}/execute", {"approval_id": approval_id}, 403)
        post(f"{task_path}/approve", original_review, 400)
        new_approval_id, _ = human_approval(task_path, [case["case_id"]])
        post(
            f"{task_path}/execute",
            {"approval_id": new_approval_id, "case_ids": [extra_case["case_id"]]},
            403,
        )
        third = post(f"{task_path}/execute", {"approval_id": new_approval_id}, 202)
        assert wait_job(task_path, third)["outcome"] == "failed"
        failed_result = get(f"{task_path}/batches/{third['execution_id']}")
        assert failed_result["report"]["accounting"]["failed"] == 1
        assert failed_result["report"]["accounting"]["passed"] == 0
        assert len(calls) == 4
        assert get(first_batch) == first_result
        later_case = {**case, "case_id": "later-batch-case"}
        post(
            f"{task_path}/cases", {"response": json.dumps({**design_output, "cases": [later_case]})}
        )
        assert get(f"{task_path}/workflow")["approval"]["stale"] is True
        assert get(first_batch) == first_result
    finally:
        release_request.set()
        target.shutdown()
        target.server_close()


def test_web_recovery_keeps_unknown_results_without_replaying(approved_setup):
    import httpx

    from api_test_agent.tasks.execution import execute_task
    from api_test_agent.ui.workflow import WebWorkflow, write_json

    store, config, approval_id, _ = approved_setup
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"items": [1]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        execute_task(store, "demo", http_client=client)
    pointer = json.loads((store.folder("demo") / "active-execution.json").read_text("utf-8"))
    execution_id = pointer["execution_id"]
    folder = store.folder("demo") / "executions" / execution_id
    # Simulate a process loss before one case's final record was persisted.
    next((folder / "records").glob("*.json")).unlink()
    store.transition("demo", "running", "Synthetic crash fixture")
    write_json(
        store.folder("demo") / "web-jobs" / "crash.json",
        {
            "job_id": "crash",
            "kind": "execute",
            "status": "running",
            "started_at": "2026-11-12T00:00:00Z",
            "execution_id": execution_id,
            "approval_id": approval_id,
        },
    )
    workflow = WebWorkflow(store)
    with pytest.raises(ValueError, match="确认"):
        workflow.post("demo", "recover", {}, None)
    workflow.post("demo", "recover", {"acknowledge": True}, None)
    result = workflow.batch("demo", execution_id)
    assert store.load("demo").status == "incomplete"
    assert result["report"]["accounting"]["missing_evidence_count"] == 1
    assert result["report"]["accounting"]["conserved"] is False
    assert result["report"]["accounting"]["not_run"] == 0
    assert len(sent) == 2


def test_web_console_rejects_public_binding(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        make_server(tmp_path, host="0.0.0.0", port=0)


def test_web_recovers_validated_design_without_model_call(prepared, monkeypatch):
    from api_test_agent.tasks import design as design_module
    from api_test_agent.ui.workflow import WebWorkflow

    store, _, output = prepared
    workflow = WebWorkflow(store)
    with monkeypatch.context() as context:

        def interrupt(*args, **kwargs):
            raise RuntimeError("simulated write interruption")

        context.setattr(design_module, "finish_import", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            workflow.post("demo", "cases", {"response": json.dumps(output)}, None)
    pending = workflow.overview("demo")["pending_imports"]
    assert len(pending) == 1
    workflow.post("demo", "recover-design", {"design_id": pending[0]}, None)
    assert workflow.overview("demo")["pending_imports"] == []
    assert len(store.cases("demo")) == 1
    assert store.load("demo").status == "designed"
