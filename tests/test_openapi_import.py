import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from api_test_agent.cli import main
from api_test_agent.tasks.discovery import Inventory, discover
from api_test_agent.tasks.models import Task
from api_test_agent.tasks.openapi_import import fetch_openapi_document, validate_openapi_url
from api_test_agent.tasks.store import TaskStore


@contextmanager
def document_server(body, status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(status)
            self.send_header("Location", "/redirected")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v2/api-docs", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("version", ["swagger", "openapi"])
def test_cli_import_url_without_repository_and_preserves_provenance(tmp_path, version):
    spec = {
        version: "2.0" if version == "swagger" else "3.0.3",
        "paths": {"/items": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    payload = json.dumps(spec)
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="url-demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            adapter="openapi",
        )
    )
    with document_server(payload.encode()) as (url, requests):
        assert (
            main(["task", "--root", str(store.root), "discover", "url-demo", "--openapi-url", url])
            == 0
        )
        assert requests == ["/v2/api-docs"]
    inv = Inventory.model_validate_json((store.root / "url-demo/inventory.json").read_text("utf-8"))
    assert len(inv.operations) == 1
    assert len(inv.operations[0].declarations) == 1
    assert inv.operations[0].key == "GET /items"
    assert not inv.version_verified and not inv.all_materials_version_verified
    assert inv.materials[0].version == "unverified-document"
    assert url in inv.materials[0].summary
    assert list(inv.snapshots.values()) == [payload]


def test_url_inventory_recovery_does_not_fetch_again(tmp_path, monkeypatch):
    from api_test_agent.tasks import discovery

    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="recover",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            openapi_url="http://example.invalid/v2/api-docs",
        )
    )
    monkeypatch.setattr(
        discovery, "fetch_openapi_document", lambda url: '{"openapi":"3.0.3","paths":{}}'
    )
    transition = store.transition

    def interrupt(*args):
        raise OSError("interrupted after inventory saved")

    monkeypatch.setattr(store, "transition", interrupt)
    with pytest.raises(OSError):
        discover(store, "recover")

    def unexpected(url):
        pytest.fail("must use saved inventory")

    monkeypatch.setattr(discovery, "fetch_openapi_document", unexpected)
    monkeypatch.setattr(store, "transition", transition)
    discover(store, "recover")
    assert store.load("recover").status == "discovered"


@pytest.mark.parametrize("status", [302, 401, 500])
def test_download_rejects_status_and_does_not_redirect(status):
    with document_server(b"not a specification", status) as (url, requests):
        with pytest.raises(ValueError, match=f"HTTP {status}"):
            fetch_openapi_document(url)
        assert requests == ["/v2/api-docs"]


def test_download_size_and_timeout(monkeypatch):
    from api_test_agent.tasks import openapi_import

    monkeypatch.setattr(openapi_import, "MAX_DOCUMENT_BYTES", 8)
    with document_server(b"123456789") as (url, _):
        with pytest.raises(ValueError, match="snapshot limit"):
            fetch_openapi_document(url)

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("private response detail")

    monkeypatch.setattr(httpx.Client, "stream", timeout)
    with pytest.raises(ValueError, match="network or timeout") as exc:
        fetch_openapi_document("http://example.invalid/spec")
    assert "private" not in str(exc.value)


def test_non_spec_url_keeps_task_created(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    with document_server(b"<html>login</html>") as (url, _):
        store.create(
            Task(
                task_id="invalid",
                target_project="synthetic",
                target_commit="a" * 40,
                scope=["*"],
                openapi_url=url,
            )
        )
        with pytest.raises(ValueError, match="not an OpenAPI"):
            discover(store, "invalid")
    assert store.load("invalid").status == "created"
    assert not (store.root / "invalid/inventory.json").exists()


def test_url_import_does_not_crawl_external_refs(tmp_path):
    spec = json.dumps(
        {
            "openapi": "3.0.3",
            "paths": {"/items": {"post": {"requestBody": {"$ref": "other.json#/Body"}}}},
        }
    )
    store = TaskStore(tmp_path / "tasks")
    with document_server(spec.encode()) as (url, requests):
        store.create(
            Task(
                task_id="refs",
                target_project="synthetic",
                target_commit="a" * 40,
                scope=["*"],
                openapi_url=url,
            )
        )
        inv = Inventory.model_validate_json(discover(store, "refs").read_text("utf-8"))
        assert requests == ["/v2/api-docs"]
    assert inv.operations[0].status == "unresolved"


def test_configure_url_validates_target_and_preserves_old_task_binding(tmp_path):
    import hashlib

    from api_test_agent.tasks.discovery import task_binding

    store = TaskStore(tmp_path / "tasks")
    task = Task(
        task_id="configured",
        target_project="synthetic",
        target_commit="a" * 40,
        scope=["*"],
        document_paths=["api.json"],
    )
    store.create(task)
    old_data = task.model_dump(mode="json", exclude={"status", "history", "openapi_url"})
    assert (
        task_binding(task)
        == hashlib.sha256(json.dumps(old_data, sort_keys=True).encode()).hexdigest()
    )
    assert (
        main(
            [
                "task",
                "--root",
                str(store.root),
                "configure",
                "configured",
                "--openapi-url",
                "http://example.invalid/v2/api-docs",
            ]
        )
        == 0
    )
    assert store.load("configured").openapi_url == "http://example.invalid/v2/api-docs"
    assert task_binding(store.load("configured")) != task_binding(task)


@pytest.mark.parametrize(
    "url",
    [
        "file:///spec.json",
        "http://user:pass@host/spec",
        "https://host/spec?token=secret",
        "http://host/spec#fragment",
    ],
)
def test_url_rejects_credentials_and_non_http(url):
    with pytest.raises(ValueError):
        validate_openapi_url(url)
