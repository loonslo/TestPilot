import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from api_test_agent.cli import main
from api_test_agent.tasks.discovery import Inventory, discover
from api_test_agent.tasks.models import Task
from api_test_agent.tasks.openapi_import import (
    DocumentBundle,
    OpenAPIImportOptions,
    fetch_openapi_document,
    snapshot_path,
    validate_openapi_url,
)
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
        discovery,
        "fetch_openapi_bundle",
        lambda url, options: DocumentBundle(
            {snapshot_path(url): '{"openapi":"3.0.3","paths":{}}'}, {snapshot_path(url): url}
        ),
    )
    transition = store.transition

    def interrupt(*args):
        raise OSError("interrupted after inventory saved")

    monkeypatch.setattr(store, "transition", interrupt)
    with pytest.raises(OSError):
        discover(store, "recover")

    def unexpected(url, options):
        pytest.fail("must use saved inventory")

    monkeypatch.setattr(discovery, "fetch_openapi_bundle", unexpected)
    monkeypatch.setattr(store, "transition", transition)
    discover(store, "recover")
    assert store.load("recover").status == "discovered"


@pytest.mark.parametrize("status", [302, 401, 500])
def test_download_rejects_status_and_does_not_redirect(status):
    with document_server(b"not a specification", status) as (url, requests):
        with pytest.raises(
            ValueError, match="redirect limit" if status == 302 else f"HTTP {status}"
        ):
            fetch_openapi_document(url, OpenAPIImportOptions(max_redirects=0))
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
                openapi_options=OpenAPIImportOptions(fetch_refs=False),
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


@contextmanager
def routed_server(routes):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, dict(self.headers)))
            status, headers, payload = routes.get(self.path, (404, {}, b"missing"))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def spec_response(spec):
    return (200, {"Content-Type": "application/json"}, json.dumps(spec).encode())


def test_large_document_default_and_configurable_limit():
    spec = {"openapi": "3.0.3", "paths": {}, "description": "x" * 1_100_000}
    with routed_server({"/spec": spec_response(spec)}) as (base, _):
        assert len(fetch_openapi_document(base + "/spec")) > 1_000_000
        with pytest.raises(ValueError, match="max_document_bytes"):
            fetch_openapi_document(base + "/spec", OpenAPIImportOptions(max_document_bytes=100))
        with pytest.raises(ValueError, match="max_total_bytes"):
            fetch_openapi_document(base + "/spec", OpenAPIImportOptions(max_total_bytes=100))


def test_authenticated_redirect_and_nested_reference_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_AUTH", "Bearer synthetic-document-credential")
    spec = {
        "openapi": "3.0.3",
        "paths": {"/items": {"post": {"requestBody": {"$ref": "body.json#/Body"}}}},
    }
    routes = {
        "/start": (302, {"Location": "/docs/spec"}, b""),
        "/docs/spec": spec_response(spec),
        "/docs/body.json": spec_response(
            {"Body": {"content": {"application/json": {"schema": {"$ref": "model.json#/Item"}}}}}
        ),
        "/docs/model.json": spec_response(
            {"Item": {"type": "object", "properties": {"name": {"type": "string"}}}}
        ),
    }
    store = TaskStore(tmp_path / "tasks")
    with routed_server(routes) as (base, seen):
        store.create(
            Task(
                task_id="auth",
                target_project="synthetic",
                target_commit="a" * 40,
                scope=["*"],
                openapi_url=base + "/start",
                openapi_options=OpenAPIImportOptions(header_env={"Authorization": "DOC_AUTH"}),
            )
        )
        inv = Inventory.model_validate_json(discover(store, "auth").read_text("utf-8"))
        assert [path for path, _ in seen] == [
            "/start",
            "/docs/spec",
            "/docs/body.json",
            "/docs/model.json",
        ]
        assert all(
            headers["Authorization"] == "Bearer synthetic-document-credential"
            for _, headers in seen
        )
        assert inv.operations[0].status == "confirmed"
        assert inv.snapshots[snapshot_path(base + "/start")] == json.dumps(spec)
        assert any(s.source_url == base + "/docs/spec" for s in inv.materials)
        assert len(inv.materials) == 3
        assert "name" in json.dumps(inv.operations[0].model_dump())
    for path in store.root.rglob("*.json"):
        assert "synthetic-document-credential" not in path.read_text("utf-8")


def test_cross_origin_redirect_requires_allowlist_and_strips_auth(monkeypatch):
    monkeypatch.setenv("DOC_AUTH", "Bearer synthetic-value")
    with routed_server({"/spec": spec_response({"openapi": "3.0.3", "paths": {}})}) as (
        other,
        received,
    ):
        with routed_server(
            {"/start": (302, {"Location": other + "/spec", "Set-Cookie": "session=synthetic"}, b"")}
        ) as (base, _):
            options = OpenAPIImportOptions(header_env={"Authorization": "DOC_AUTH"})
            with pytest.raises(ValueError, match="origin not allowed"):
                fetch_openapi_document(base + "/start", options)
            assert not received
            fetch_openapi_document(
                base + "/start",
                OpenAPIImportOptions(
                    header_env={"Authorization": "DOC_AUTH"},
                    allowed_origins=[other],
                ),
            )
    assert len(received) == 1
    assert "Authorization" not in received[0][1]
    assert "Cookie" not in received[0][1]


def test_missing_auth_fails_without_request(monkeypatch):
    monkeypatch.delenv("ABSENT_DOC_AUTH", raising=False)
    with routed_server({}) as (base, seen):
        with pytest.raises(ValueError, match="ABSENT_DOC_AUTH"):
            fetch_openapi_document(
                base + "/spec",
                OpenAPIImportOptions(
                    header_env={"Authorization": "ABSENT_DOC_AUTH"},
                ),
            )
        assert not seen


@pytest.mark.parametrize("kind", ["springfox", "swagger-config"])
def test_aggregation_resource_list(tmp_path, kind):
    from api_test_agent.tasks.openapi_import import fetch_openapi_bundle

    members = [{"name": "one", "url": "/one"}, {"name": "two", "location": "/two"}]
    root = members if kind == "springfox" else {"urls": members}
    routes = {
        "/resources": spec_response(root),
        "/one": spec_response({"swagger": "2.0", "paths": {"/one": {"get": {}}}}),
        "/two": spec_response({"openapi": "3.0.3", "paths": {"/two": {"post": {}}}}),
    }
    with routed_server(routes) as (base, seen):
        store = TaskStore(tmp_path / "tasks")
        store.create(
            Task(
                task_id="aggregate",
                target_project="synthetic",
                target_commit="a" * 40,
                scope=["*"],
                openapi_url=base + "/resources",
            )
        )
        inv = Inventory.model_validate_json(discover(store, "aggregate").read_text("utf-8"))
        assert {op.key for op in inv.operations} == {"GET /one", "POST /two"}
        assert len(seen) == 3
        with pytest.raises(ValueError, match="max_documents"):
            fetch_openapi_bundle(base + "/resources", OpenAPIImportOptions(max_documents=1))


def test_missing_aggregation_member_aborts():
    from api_test_agent.tasks.openapi_import import fetch_openapi_bundle

    with routed_server({"/resources": spec_response([{"url": "/missing"}])}) as (base, _):
        with pytest.raises(ValueError, match="HTTP 404"):
            fetch_openapi_bundle(base + "/resources")


def test_ref_cycle_is_bounded_and_unavailable_ref_is_visible(tmp_path):
    routes = {
        "/spec": spec_response(
            {
                "openapi": "3.0.3",
                "paths": {"/items": {"post": {"requestBody": {"$ref": "/body#/Body"}}}},
            }
        ),
        "/body": spec_response({"Body": {"$ref": "/spec#/paths/~1items/post/requestBody"}}),
    }
    with routed_server(routes) as (base, seen):
        store = TaskStore(tmp_path / "tasks")
        store.create(
            Task(
                task_id="cycle",
                target_project="synthetic",
                target_commit="a" * 40,
                scope=["*"],
                openapi_url=base + "/spec",
            )
        )
        inv = Inventory.model_validate_json(discover(store, "cycle").read_text("utf-8"))
        assert inv.operations[0].status == "unresolved"
        assert len(seen) == 2


def test_configure_import_options_and_binding(tmp_path):
    from api_test_agent.tasks.discovery import task_binding

    store = TaskStore(tmp_path / "tasks")
    store.create(Task(task_id="options"))
    before = task_binding(store.load("options"))
    config = tmp_path / "import.toml"
    config.write_text(
        'max_document_bytes = 30000000\n[header_env]\nAuthorization = "DOC_AUTH"\n', "utf-8"
    )
    assert (
        main(
            [
                "task",
                "--root",
                str(store.root),
                "configure",
                "options",
                "--openapi-config",
                str(config),
            ]
        )
        == 0
    )
    task = store.load("options")
    assert task.openapi_options.max_document_bytes == 30000000
    assert task.openapi_options.header_env == {"Authorization": "DOC_AUTH"}
    assert task_binding(task) != before


def test_one_command_import_and_unavailable_dependency(tmp_path):
    routes = {
        "/spec": spec_response(
            {
                "openapi": "3.0.3",
                "paths": {"/items": {"post": {"requestBody": {"$ref": "/missing#/Body"}}}},
            }
        ),
    }
    root = tmp_path / "tasks"
    with routed_server(routes) as (base, seen):
        assert (
            main(
                [
                    "task",
                    "--root",
                    str(root),
                    "import-openapi",
                    "onboard",
                    "--project",
                    "synthetic",
                    "--commit",
                    "a" * 40,
                    "--url",
                    base + "/spec",
                ]
            )
            == 0
        )
        assert [p for p, _ in seen] == ["/spec", "/missing"]
    inv = Inventory.model_validate_json((root / "onboard/inventory.json").read_text("utf-8"))
    assert inv.operations[0].status == "unresolved"
    assert any("reference unavailable" in warning for warning in inv.limitations)


def test_unapproved_reference_origin_never_contacted(tmp_path):
    with routed_server({}) as (other, other_seen):
        with routed_server(
            {
                "/spec": spec_response(
                    {
                        "swagger": "2.0",
                        "paths": {
                            "/items": {
                                "get": {"responses": {"200": {"$ref": other + "/body#/Body"}}}
                            }
                        },
                    }
                )
            }
        ) as (base, _):
            store = TaskStore(tmp_path / "tasks")
            store.create(
                Task(
                    task_id="origins",
                    target_project="synthetic",
                    target_commit="a" * 40,
                    scope=["*"],
                    openapi_url=base + "/spec",
                )
            )
            inv = Inventory.model_validate_json(discover(store, "origins").read_text("utf-8"))
            assert inv.operations[0].status == "unresolved"
            assert any("origin not allowed" in warning for warning in inv.limitations)
            assert not other_seen


def test_deadline_and_redirect_loops_are_bounded():
    with routed_server({"/loop": (302, {"Location": "/loop"}, b"")}) as (base, seen):
        with pytest.raises(ValueError, match="redirect limit"):
            fetch_openapi_document(base + "/loop", OpenAPIImportOptions(max_redirects=2))
        assert len(seen) == 3
        with pytest.raises(ValueError, match="total_timeout_seconds"):
            fetch_openapi_document(base + "/loop", OpenAPIImportOptions(total_timeout_seconds=1e-9))
        assert len(seen) == 3
