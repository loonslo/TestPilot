"""Lightweight built-in Web console server for TestPilot.

Zero-dependency HTTP server delivering:
- Interactive dashboard UI (hand-written CSS, no CDN, strict CSP)
- Real-time pipeline state (Task -> Inventory -> Cases -> Execution -> Report)
- Complete process artifacts inspection (Sources drawer)
- Safe offline model connectivity checks
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from api_test_agent.adapters import get_all_supported_extensions, list_adapters
from api_test_agent.envvars import read_env
from api_test_agent.tasks.discovery import discover
from api_test_agent.tasks.models import Task
from api_test_agent.tasks.store import TaskStore

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent


def _json_default(value: Any) -> str:
    """Serialize the contract models' timestamps instead of dropping the response."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def validate_task_id(task_id: str) -> None:
    if not task_id or not re.fullmatch(r"^[a-zA-Z0-9_.-]{1,64}$", task_id) or ".." in task_id:
        raise ValueError(f"Invalid or unsafe task_id: {task_id!r}")


class UIRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the TestPilot Web Console."""

    store: TaskStore
    static_dir: Path = STATIC_DIR

    def _apply_cors(self) -> None:
        """Reflect only an exact same-origin request.

        A loopback host alone is not enough: any other local page on a different
        port is a different origin and must not get a readable CORS grant.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return
        parsed = urllib.parse.urlsplit(origin)
        server_port = getattr(self.server, "server_port", None)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return
        if not server_port or parsed.port != server_port:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")

    def _token_ok(self) -> bool:
        server_token = getattr(self.server, "auth_token", "")
        if not server_token:
            return True
        client_token = (
            self.headers.get("X-TestPilot-Token")
            or self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        )
        return secrets.compare_digest(client_token, server_token)

    def _check_read_auth(self) -> bool:
        """Session token for read endpoints that expose host filesystem or artifacts."""
        if not self._token_ok():
            self._send_error(
                "Forbidden: invalid or missing X-TestPilot-Token session credential",
                status=HTTPStatus.FORBIDDEN,
            )
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self._send_error("Forbidden: cross-site request blocked", status=HTTPStatus.FORBIDDEN)
            return False
        return True

    def _is_allowed_path(self, target: Path) -> bool:
        for root in getattr(self.server, "allowed_roots", ()):
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _check_mutation_auth(self) -> bool:
        """Enforce strict host, origin, and session token checks for POST mutations."""
        # 1. Host header validation
        host = self.headers.get("Host", "")
        host_name = host.split(":")[0].strip("[]")
        if host_name not in {"127.0.0.1", "localhost", "::1"}:
            self._send_error(f"Forbidden: untrusted Host {host!r}", status=HTTPStatus.FORBIDDEN)
            return False

        # 2. Origin check: If present, must be loopback and match server port
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                self._send_error(
                    f"Forbidden: untrusted Origin {origin!r}", status=HTTPStatus.FORBIDDEN
                )
                return False
            server_port = getattr(self.server, "server_port", None)
            if server_port and parsed.port and parsed.port != server_port:
                self._send_error(
                    f"Forbidden: cross-port Origin {origin!r} does not match "
                    f"server port {server_port}",
                    status=HTTPStatus.FORBIDDEN,
                )
                return False

        # 3. Sec-Fetch-Site check
        sec_fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if sec_fetch_site == "cross-site":
            self._send_error("Forbidden: cross-site request blocked", status=HTTPStatus.FORBIDDEN)
            return False

        # 4. Session Token validation
        if not self._token_ok():
            self._send_error(
                "Forbidden: invalid or missing X-TestPilot-Token session credential",
                status=HTTPStatus.FORBIDDEN,
            )
            return False

        return True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep console output concise and clean
        sys.stderr.write(f"[TestPilot UI] {self.address_string()} - {format % args}\n")

    def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2, default=_json_default).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self._apply_cors()
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(
        self,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
        status: int = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        for header, value in (extra_headers or {}).items():
            self.send_header(header, value)
        self._apply_cors()
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status=status)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._apply_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-TestPilot-Token")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: C901, PLR0912
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        # 0. Auth Token Endpoint
        if path == "/api/auth/token":
            # Only a page served from this console (or a direct local tool) may read
            # the session token; a cross-site page must not be able to lift it.
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                self._send_error(
                    "Forbidden: cross-site request blocked", status=HTTPStatus.FORBIDDEN
                )
                return
            origin = self.headers.get("Origin")
            if origin:
                parsed_origin = urllib.parse.urlsplit(origin)
                server_port = getattr(self.server, "server_port", None)
                if (
                    parsed_origin.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or not server_port
                    or parsed_origin.port != server_port
                ):
                    self._send_error(
                        "Forbidden: cross-origin token request", status=HTTPStatus.FORBIDDEN
                    )
                    return
            self._send_json({"token": getattr(self.server, "auth_token", "")})
            return

        # 1. Static Assets
        if path in {"/", "/dashboard.html", "/index.html"}:
            html_file = self.static_dir / "dashboard.html"
            if not html_file.is_file():
                self._send_error("Dashboard template not found", status=HTTPStatus.NOT_FOUND)
                return
            html_content = html_file.read_text("utf-8")
            token = getattr(self.server, "auth_token", "")
            injected_meta = (
                f'<meta name="testpilot-token" content="{token}">\n'
                f'<script>window.TESTPILOT_TOKEN="{token}";</script>\n'
            )
            html_content = html_content.replace("<head>", f"<head>\n{injected_meta}", 1)
            self._send_text(
                html_content,
                content_type="text/html; charset=utf-8",
                extra_headers={
                    "Content-Security-Policy": (
                        "default-src 'self'; "
                        "script-src 'self' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline'; "
                        "img-src 'self' data:; "
                        "connect-src 'self'; "
                        "font-src 'self'; "
                        "frame-ancestors 'none'; "
                        "base-uri 'none'; "
                        "form-action 'none'"
                    ),
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                },
            )
            return

        # 1.5 File System Browser (token-gated, confined to allowed roots)
        if path == "/api/fs/list":
            if not self._check_read_auth():
                return
            query_params = urllib.parse.parse_qs(parsed.query)
            self._handle_fs_list(query_params)
            return

        # 2. Registered Adapters
        if path == "/api/adapters":
            self._send_json(
                {
                    "adapters": list_adapters(),
                    "extensions": sorted(get_all_supported_extensions()),
                }
            )
            return

        # 3. Environment Variables Presence (Boolean only, never exposes values)
        if path == "/api/models/env":
            self._send_json(
                {
                    "DEEPSEEK_API_KEY": bool(read_env("DEEPSEEK_API_KEY")),
                    "API_TEST_AGENT_MODEL_API_KEY": bool(read_env("API_TEST_AGENT_MODEL_API_KEY")),
                }
            )
            return

        # 4. List Tasks
        if path == "/api/tasks":
            tasks_data = []
            for task in self.store.list_tasks():
                folder = self.store.root / task.task_id
                inv_path = folder / "inventory.json"
                inv_count = 0
                if inv_path.is_file():
                    try:
                        inv_data = json.loads(inv_path.read_text("utf-8"))
                        inv_count = len(inv_data.get("operations", []))
                    except Exception:
                        pass
                cases = self.store.cases(task.task_id)
                reviews = self.store.review_history(task.task_id)
                has_exec = (folder / "active-execution.json").is_file()
                has_report = (folder / "report.md").is_file()

                tasks_data.append(
                    {
                        "task_id": task.task_id,
                        "status": task.status,
                        "target_project": task.target_project,
                        "target_commit": task.target_commit,
                        "adapter": task.adapter,
                        "scope": list(task.scope),
                        "repository": task.repository,
                        "document_paths": list(task.document_paths),
                        "openapi_url": task.openapi_url,
                        "source_paths": list(task.source_paths),
                        "environment": task.environment,
                        "inventory_count": inv_count,
                        "cases_count": len(cases),
                        "review_count": len(reviews),
                        "has_execution": has_exec,
                        "has_report": has_report,
                    }
                )
            self._send_json({"tasks": tasks_data})
            return

        # 5. Task Detail & Sub-resources
        if path.startswith("/api/tasks/"):
            parts = path.split("/")[3:]  # ["task_id", ...]
            if not parts or not parts[0]:
                self._send_error("Task ID required", status=HTTPStatus.BAD_REQUEST)
                return

            task_id = parts[0]
            try:
                validate_task_id(task_id)
            except ValueError as exc:
                self._send_error(str(exc), status=HTTPStatus.BAD_REQUEST)
                return
            try:
                task = self.store.load(task_id)
            except Exception as exc:
                self._send_error(f"Task '{task_id}' not found: {exc}", status=HTTPStatus.NOT_FOUND)
                return

            folder = self.store.root / task_id

            # /api/tasks/<task_id>
            if len(parts) == 1:
                self._send_json(task.model_dump())
                return

            sub = parts[1]

            # /api/tasks/<task_id>/inventory
            if sub == "inventory":
                inv_file = folder / "inventory.json"
                if not inv_file.is_file():
                    self._send_error("Inventory not generated yet", status=HTTPStatus.NOT_FOUND)
                    return
                self._send_text(
                    inv_file.read_text("utf-8"), content_type="application/json; charset=utf-8"
                )
                return

            # /api/tasks/<task_id>/cases
            if sub == "cases":
                cases = [c.model_dump() for c in self.store.cases(task_id)]
                self._send_json({"cases": cases})
                return

            # /api/tasks/<task_id>/reviews
            if sub == "reviews":
                reviews = [r.model_dump() for r in self.store.review_history(task_id)]
                self._send_json({"reviews": reviews})
                return

            # /api/tasks/<task_id>/execution
            if sub == "execution":
                records = [r.model_dump() for r in self.store.execution_records(task_id)]
                active_file = folder / "active-execution.json"
                active_meta = {}
                if active_file.is_file():
                    try:
                        active_meta = json.loads(active_file.read_text("utf-8"))
                    except Exception:
                        pass
                self._send_json({"active": active_meta, "records": records})
                return

            # /api/tasks/<task_id>/report
            if sub == "report":
                report_md = ""
                report_json: dict[str, Any] = {}
                md_path = folder / "report.md"
                json_path = folder / "report.json"
                if md_path.is_file():
                    report_md = md_path.read_text("utf-8")
                if json_path.is_file():
                    try:
                        report_json = json.loads(json_path.read_text("utf-8"))
                    except Exception:
                        pass
                self._send_json({"markdown": report_md, "report": report_json})
                return

            # /api/tasks/<task_id>/evidence/<evidence_id>
            if sub == "evidence" and len(parts) >= 3:
                evidence_id = parts[2]
                ev = self.store.load_evidence(task_id, evidence_id)
                if ev is None:
                    self._send_error(
                        f"Evidence '{evidence_id}' not found", status=HTTPStatus.NOT_FOUND
                    )
                    return
                self._send_json(ev.model_dump())
                return

            # /api/tasks/<task_id>/source/<type> (Sources Drawer inspection)
            if sub == "source" and len(parts) >= 3:
                src_type = parts[2]
                self._handle_source_inspection(task_id, folder, src_type)
                return

        self._send_error(f"Unknown endpoint: {path}", status=HTTPStatus.NOT_FOUND)

    def _handle_fs_list(self, query_params: dict[str, list[str]]) -> None:
        raw_path = query_params.get("path", [""])[0].strip()
        filter_mode = query_params.get("filter", ["all"])[0].strip().lower()

        workspace_root = getattr(self.server, "workspace_root", Path.cwd().resolve())
        if raw_path:
            # Accept absolute or root-relative input, then confine the result.
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = workspace_root / candidate
            target_path = candidate.resolve()
        else:
            target_path = workspace_root

        if not self._is_allowed_path(target_path):
            self._send_error(
                "Forbidden: path is outside the console's allowed roots",
                status=HTTPStatus.FORBIDDEN,
            )
            return

        if not target_path.exists() or not target_path.is_dir():
            target_path = workspace_root

        parent_path = (
            str(target_path.parent).replace("\\", "/") if target_path.parent != target_path else ""
        )

        quick_dirs = [
            {"name": "工作区根目录", "path": str(workspace_root).replace("\\", "/")},
            {
                "name": "被测源码 (subjects)",
                "path": str((workspace_root / "subjects").resolve()).replace("\\", "/"),
            },
            {
                "name": "示例任务 (examples)",
                "path": str((workspace_root / "examples").resolve()).replace("\\", "/"),
            },
            {
                "name": "基准规格 (benchmarks)",
                "path": str((workspace_root / "benchmarks").resolve()).replace("\\", "/"),
            },
            {
                "name": "任务资产 (runtime/tasks)",
                "path": str((workspace_root / "runtime" / "tasks").resolve()).replace("\\", "/"),
            },
        ]
        quick_dirs = [q for q in quick_dirs if Path(q["path"]).exists()]
        quick_dirs = [q for q in quick_dirs if self._is_allowed_path(Path(q["path"]))]

        items: list[dict[str, Any]] = []
        try:
            with os.scandir(target_path) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith(".") or name in {"__pycache__", "node_modules"}:
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        size = entry.stat(follow_symlinks=False).st_size if not is_dir else 0
                    except OSError, PermissionError:
                        continue

                    entry_resolved = Path(entry.path).resolve()
                    if not self._is_allowed_path(entry_resolved):
                        continue
                    entry_full_path = str(entry_resolved).replace("\\", "/")
                    ext = Path(name).suffix.lower()
                    is_openapi = ext in {".json", ".yaml", ".yml"}
                    is_code = ext in {".py", ".java", ".ts", ".js", ".go", ".rs"}

                    if filter_mode == "dir" and not is_dir:
                        continue
                    if filter_mode == "doc" and not (is_dir or is_openapi):
                        continue
                    if filter_mode == "code" and not (is_dir or is_code):
                        continue

                    items.append(
                        {
                            "name": name,
                            "path": entry_full_path,
                            "is_dir": is_dir,
                            "size": size,
                            "ext": ext,
                            "is_openapi": is_openapi,
                            "is_code": is_code,
                        }
                    )
        except (OSError, PermissionError) as exc:
            self._send_error(f"Cannot read directory: {exc}", status=HTTPStatus.FORBIDDEN)
            return

        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

        self._send_json(
            {
                "current_path": str(target_path).replace("\\", "/"),
                "parent_path": parent_path,
                "workspace_root": str(workspace_root).replace("\\", "/"),
                "filter": filter_mode,
                "quick_dirs": quick_dirs,
                "items": items,
            }
        )

    def _handle_source_inspection(self, task_id: str, folder: Path, src_type: str) -> None:
        rel_base = f"runtime/tasks/{task_id}"
        if src_type == "task":
            f = folder / "task.json"
            content = f.read_text("utf-8") if f.is_file() else "{}"
            self._send_json(
                {
                    "title": "Task 目标契约",
                    "path": f"{rel_base}/task.json",
                    "badge": "task.json",
                    "content": content,
                }
            )
            return

        if src_type == "inventory":
            f = folder / "inventory.json"
            content = (
                f.read_text("utf-8")
                if f.is_file()
                else json.dumps({"message": "尚未执行接口发现 (未生成 inventory.json)"}, indent=2)
            )
            self._send_json(
                {
                    "title": "Inventory 接口资产清单",
                    "path": f"{rel_base}/inventory.json",
                    "badge": "inventory.json",
                    "content": content,
                }
            )
            return

        if src_type == "cases":
            cases = [c.model_dump() for c in self.store.cases(task_id)]
            self._send_json(
                {
                    "title": "Cases 结构化用例集",
                    "path": f"{rel_base}/cases/",
                    "badge": "cases.json",
                    "content": json.dumps(cases, indent=2, ensure_ascii=False),
                }
            )
            return

        if src_type == "execution":
            records = [r.model_dump() for r in self.store.execution_records(task_id)]
            f = folder / "active-execution.json"
            meta = json.loads(f.read_text("utf-8")) if f.is_file() else {}
            exec_dump = {"active": meta, "records": records}
            self._send_json(
                {
                    "title": "Execution 执行与证据记录",
                    "path": f"{rel_base}/execution.json",
                    "badge": "execution.json",
                    "content": json.dumps(exec_dump, indent=2, ensure_ascii=False),
                }
            )
            return

        if src_type == "report":
            f = folder / "report.md"
            content = (
                f.read_text("utf-8")
                if f.is_file()
                else "# 报告尚未生成\n\n请在执行测试用例后生成守恒对账与执行总结报告。"
            )
            self._send_json(
                {
                    "title": "Report 测试报告 Markdown",
                    "path": f"{rel_base}/report.md",
                    "badge": "report.md",
                    "content": content,
                }
            )
            return

        self._send_error(f"Unknown source type: {src_type}", status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: C901, PLR0912
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            data = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError as exc:
            self._send_error(f"Malformed JSON body: {exc}", status=HTTPStatus.BAD_REQUEST)
            return

        # 0. UI Execution Guard
        if "execute" in path.split("/"):
            self._send_error(
                "UI execution guard rejected: Web UI is strictly limited to pipeline inspection. "
                "Execute via CLI: testpilot task execute",
                status=HTTPStatus.FORBIDDEN,
            )
            return

        # 0.1 Strict Origin, Host & Token Authorization
        if not self._check_mutation_auth():
            return

        # 1. Safe Ping (No paid model inference, zero key leak)
        if path == "/api/model/ping":
            base_url = data.get("base_url", "").strip()
            key_env = data.get("api_key_env", "").strip()

            if not base_url:
                self._send_error("base_url is required for ping")
                return

            key_found = bool(read_env(key_env)) if key_env else False
            ping_result = _test_url_reachability(base_url)
            self._send_json(
                {
                    "ok": ping_result["ok"],
                    "latency_ms": ping_result["latency_ms"],
                    "status_code": ping_result["status_code"],
                    "key_configured": key_found,
                    "message": ping_result["message"],
                }
            )
            return

        # 2. Create Task
        if path == "/api/tasks/create":
            task_id = data.get("task_id", "").strip()
            try:
                validate_task_id(task_id)
            except ValueError as exc:
                self._send_error(str(exc), status=HTTPStatus.BAD_REQUEST)
                return
            try:
                task = Task(
                    task_id=task_id,
                    target_project=data.get("target_project") or None,
                    target_commit=data.get("target_commit") or None,
                    scope=tuple(data.get("scope", [])),
                    repository=data.get("repository") or None,
                    adapter=data.get("adapter") or None,
                    document_paths=tuple(data.get("document_paths", [])),
                    openapi_url=data.get("openapi_url") or None,
                    openapi_options=data.get("openapi_options") or None,
                    source_paths=tuple(data.get("source_paths", [])),
                    environment=data.get("environment") or None,
                    version_manifest=data.get("version_manifest") or {},
                )
                self.store.create(task)
                self._send_json(task.model_dump(), status=HTTPStatus.CREATED)
            except (ValueError, ValidationError) as exc:
                self._send_error(f"Failed to create task: {exc}", status=HTTPStatus.BAD_REQUEST)
            return

        # 3. Configure Task
        if path.startswith("/api/tasks/") and path.endswith("/configure"):
            task_id = path.split("/")[3]
            try:
                validate_task_id(task_id)
            except ValueError as exc:
                self._send_error(str(exc), status=HTTPStatus.BAD_REQUEST)
                return
            try:
                allowed_keys = {
                    "target_project",
                    "target_commit",
                    "scope",
                    "repository",
                    "source_paths",
                    "document_paths",
                    "openapi_url",
                    "openapi_options",
                    "environment",
                    "adapter",
                    "version_manifest",
                }
                updates = {k: v for k, v in data.items() if k in allowed_keys}
                if "scope" in updates and isinstance(updates["scope"], list):
                    updates["scope"] = tuple(updates["scope"])
                if "source_paths" in updates and isinstance(updates["source_paths"], list):
                    updates["source_paths"] = tuple(updates["source_paths"])
                if "document_paths" in updates and isinstance(updates["document_paths"], list):
                    updates["document_paths"] = tuple(updates["document_paths"])

                updated = self.store.configure(task_id, **updates)
                self._send_json(updated.model_dump())
            except Exception as exc:
                self._send_error(f"Failed to configure task: {exc}", status=HTTPStatus.BAD_REQUEST)
            return

        # 4. Discover Operations
        if path.startswith("/api/tasks/") and path.endswith("/discover"):
            task_id = path.split("/")[3]
            try:
                validate_task_id(task_id)
            except ValueError as exc:
                self._send_error(str(exc), status=HTTPStatus.BAD_REQUEST)
                return
            try:
                materials_root = getattr(self.server, "materials_root", None) or Path.cwd()
                inventory_path = discover(self.store, task_id, materials_root=materials_root)
                inv_text = inventory_path.read_text("utf-8")
                self._send_text(inv_text, content_type="application/json; charset=utf-8")
            except Exception as exc:
                self._send_error(f"Discovery failed: {exc}", status=HTTPStatus.BAD_REQUEST)
            return

        self._send_error(f"Unknown POST endpoint: {path}", status=HTTPStatus.NOT_FOUND)


def _test_url_reachability(url: str, timeout: float = 3.0) -> dict[str, Any]:
    """Safe connectivity check to an HTTP endpoint with zero key exposure."""
    start = time.perf_counter()
    try:
        parsed = urllib.parse.urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return {
                "ok": False,
                "latency_ms": 0.0,
                "status_code": 0,
                "message": f"Invalid URL structure: {url}",
            }

        # Try a quick socket handshake first to avoid blocking on hung HTTP requests
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "TestPilot-HealthCheck/1.0"},
            method="HEAD",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed = (time.perf_counter() - start) * 1000
                return {
                    "ok": True,
                    "latency_ms": round(elapsed, 1),
                    "status_code": resp.status,
                    "message": f"HTTP {resp.status} OK",
                }
        except urllib.error.HTTPError as exc:
            # 401/403/404/405 still confirms the endpoint is alive and responding
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "ok": True,
                "latency_ms": round(elapsed, 1),
                "status_code": exc.code,
                "message": f"Endpoint reachable (HTTP {exc.code})",
            }
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "ok": False,
            "latency_ms": round(elapsed, 1),
            "status_code": 0,
            "message": f"Connection failed: {exc}",
        }


def make_server(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    auth_token: str | None = None,
    allowed_roots: tuple[Path, ...] = (),
) -> ThreadingHTTPServer:
    """Instantiate a configured ThreadingHTTPServer bound to the given root."""
    store = TaskStore(root)
    token = auth_token or secrets.token_hex(16)
    workspace_root = Path.cwd().resolve()
    roots = tuple(dict.fromkeys([workspace_root, *(p.resolve() for p in allowed_roots)]))

    class CustomHandler(UIRequestHandler):
        pass

    CustomHandler.store = store
    CustomHandler.static_dir = STATIC_DIR

    server = ThreadingHTTPServer((host, port), CustomHandler)
    server.auth_token = token
    server.workspace_root = workspace_root
    server.allowed_roots = roots
    return server


def run_server(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
    allowed_roots: tuple[Path, ...] = (),
) -> None:
    """Run the Web console server loop."""
    server = make_server(root, host, port, allowed_roots=allowed_roots)
    url = f"http://{host}:{port}/"
    print(f"TestPilot Web 控制台已启动: {url}")
    print(f"数据根目录: {root.resolve()}")
    print("按 Ctrl+C 停止服务器\n")

    if open_browser:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 TestPilot 控制台服务器...")
    finally:
        server.server_close()


def register_ui_command(commands: argparse._SubParsersAction) -> None:
    """Register the `ui` subcommand under testpilot."""
    parser = commands.add_parser("ui", help="启动可视化测试控制台与过程文件透视面板")
    parser.add_argument("--port", type=int, default=8080, help="Web 服务器端口 (默认 8080)")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址 (默认 127.0.0.1)")
    parser.add_argument("--root", type=Path, default=Path("runtime/tasks"), help="任务资产根目录")
    parser.add_argument("--no-open", action="store_true", help="启动时不自动打开浏览器")
    parser.add_argument(
        "--allow-root",
        action="append",
        type=Path,
        default=[],
        metavar="DIR",
        help="额外允许文件浏览器访问的根目录（可重复）；工作目录始终允许",
    )
    parser.set_defaults(ui_handler=execute_ui_command)


def execute_ui_command(args: argparse.Namespace) -> int:
    """Execute handler for `testpilot ui`."""
    args.root.mkdir(parents=True, exist_ok=True)
    run_server(
        root=args.root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        allowed_roots=tuple(args.allow_root),
    )
    return 0
