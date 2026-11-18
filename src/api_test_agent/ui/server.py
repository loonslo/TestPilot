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
from uuid import uuid4

from api_test_agent.adapters import get_all_supported_extensions, list_adapters
from api_test_agent.envvars import read_env
from api_test_agent.tasks.discovery import SECRET_PATH_PATTERN, discover, document_permitted, git
from api_test_agent.tasks.models import Task
from api_test_agent.tasks.store import TaskStore
from api_test_agent.ui.workflow import BusyTask, WebWorkflow, safe_error

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
    workflow: WebWorkflow

    def _token_ok(self) -> bool:
        server_token = getattr(self.server, "auth_token", "")
        if not server_token:
            return True
        client_token = (
            self.headers.get("X-TestPilot-Token")
            or self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        )
        return secrets.compare_digest(client_token.encode(), server_token.encode())

    def _check_origin(self) -> bool:
        """Require a local host and, for browser requests, the console origin."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self._send_error("Forbidden: cross-site request blocked", status=HTTPStatus.FORBIDDEN)
            return False
        try:
            parsed_host = urllib.parse.urlsplit(f"http://{host}")
            parsed_origin = urllib.parse.urlsplit(origin) if origin else None
            port = self.server.server_port
            trusted = (
                parsed_host.hostname in {"127.0.0.1", "localhost", "::1"}
                and (parsed_host.port or 80) == port
                and not parsed_host.username
                and not parsed_host.password
                and not parsed_host.path
                and not parsed_host.query
                and not parsed_host.fragment
                and (
                    parsed_origin is None
                    or (
                        parsed_origin.scheme == "http"
                        and parsed_origin.hostname == parsed_host.hostname
                        and (parsed_origin.port or 80) == port
                        and not parsed_origin.username
                        and not parsed_origin.password
                        and not parsed_origin.path
                        and not parsed_origin.query
                        and not parsed_origin.fragment
                    )
                )
            )
        except ValueError:
            trusted = False
        if not trusted:
            self._send_error("Forbidden: untrusted Host or Origin", status=HTTPStatus.FORBIDDEN)
        return trusted

    def _check_read_auth(self) -> bool:
        if not self._token_ok():
            self._send_error(
                "Forbidden: invalid or missing X-TestPilot-Token session credential",
                status=HTTPStatus.FORBIDDEN,
            )
            return False
        return True

    def _is_allowed_path(self, target: Path) -> bool:
        if not target.is_absolute() or target.drive.startswith(("\\", "//")):
            return False
        workspace = getattr(self.server, "workspace_root", Path.cwd().resolve())
        subjects = workspace / "subjects"
        if target.is_relative_to(subjects) and any(
            part.lower() in {"data", "backups", "backup"}
            for part in target.relative_to(subjects).parts
        ):
            return False
        return not any(
            part.startswith(".") or SECRET_PATH_PATTERN.search(part) for part in target.parts[1:]
        )

    def _validate_local_materials(self, task: Task) -> None:
        root = getattr(self.server, "workspace_root", Path.cwd())
        if task.repository and not self._is_allowed_path((root / task.repository).resolve()):
            raise PermissionError("Repository path is outside the permitted local sources")
        if task.source_directory:
            directory = root / task.source_directory
            if not self._is_allowed_path(directory) or not self._is_allowed_path(
                directory.resolve()
            ):
                raise PermissionError("Source directory is outside the permitted local sources")
        for document in task.document_paths:
            path = (root / document).resolve()
            if not self._is_allowed_path(path) or not document_permitted(path):
                raise PermissionError("Document path is outside the permitted local sources")

    def _workflow_error(self, exc: Exception) -> None:
        if isinstance(exc, BusyTask):
            status = HTTPStatus.CONFLICT
        elif isinstance(exc, FileNotFoundError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(exc, PermissionError):
            status = HTTPStatus.FORBIDDEN
        elif isinstance(exc, (ValueError, OSError)):
            status = HTTPStatus.BAD_REQUEST
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_error(safe_error(exc), status=status)

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
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status=status)

    def do_OPTIONS(self) -> None:
        if not self._check_origin():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-TestPilot-Token")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: C901, PLR0912
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        if not self._check_origin():
            return
        if path == "/api/auth/token":
            self._send_json({"token": getattr(self.server, "auth_token", "")})
            return
        if path.startswith("/api/") and not self._check_read_auth():
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

        # 1.5 Local File System Browser
        if path == "/api/fs/list":
            query_params = urllib.parse.parse_qs(parsed.query)
            self._handle_fs_list(query_params)
            return

        if path == "/api/model/config":
            try:
                self._send_json(self.workflow.model_config())
            except Exception as exc:
                self._workflow_error(exc)
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
                        "source_directory": task.source_directory,
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

            folder = self.store.folder(task_id)

            # /api/tasks/<task_id>
            if len(parts) == 1:
                self._send_json(task.model_dump())
                return

            sub = parts[1]

            if sub in {"workflow", "batches"}:
                try:
                    if sub == "workflow" and len(parts) == 2:
                        payload = self.workflow.overview(task_id)
                    elif sub == "batches" and len(parts) == 2:
                        payload = {"batches": self.workflow.batches(task_id)}
                    elif sub == "batches" and len(parts) == 3:
                        payload = self.workflow.batch(task_id, parts[2])
                    elif sub == "batches" and len(parts) == 5 and parts[3] == "evidence":
                        if not re.fullmatch(r"[0-9a-f]{32}", parts[2]) or not re.fullmatch(
                            r"[0-9a-f]{16,64}", parts[4]
                        ):
                            raise FileNotFoundError("执行批次不存在")
                        evidence = self.store.load_evidence(task_id, parts[4], parts[2])
                        if evidence is None:
                            raise FileNotFoundError("批次证据不存在")
                        payload = evidence.model_dump()
                    else:
                        raise FileNotFoundError("Unknown workflow endpoint")
                    self._send_json(payload)
                except Exception as exc:
                    self._workflow_error(exc)
                return

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
                active_path = folder / "active-execution.json"
                if active_path.is_file():
                    active_id = json.loads(active_path.read_text("utf-8")).get("execution_id", "")
                    if re.fullmatch(r"[0-9a-f]{32}", active_id):
                        cached = folder / "executions" / active_id / "report"
                        if (cached / "report.md").is_file() and (cached / "report.json").is_file():
                            self._send_json(
                                {
                                    "markdown": (cached / "report.md").read_text("utf-8"),
                                    "report": json.loads(
                                        (cached / "report.json").read_text("utf-8")
                                    ),
                                }
                            )
                            return
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
                ev = (
                    self.store.load_evidence(task_id, evidence_id)
                    if re.fullmatch(r"[0-9a-f]{16,64}", evidence_id)
                    else None
                )
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
            # Accept absolute or workspace-relative input.
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = workspace_root / candidate
            try:
                target_path = candidate.resolve()
            except (OSError, ValueError) as exc:
                self._workflow_error(exc)
                return
        else:
            target_path = workspace_root

        if not self._is_allowed_path(target_path):
            self._send_error(
                "Forbidden: path is outside the permitted local sources",
                status=HTTPStatus.FORBIDDEN,
            )
            return

        if not target_path.exists() or not target_path.is_dir():
            self._send_error("Directory does not exist", status=HTTPStatus.NOT_FOUND)
            return

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
        ]
        drives = os.listdrives() if hasattr(os, "listdrives") else [target_path.anchor]
        quick_dirs.extend(
            {"name": drive.replace("\\", "/"), "path": drive.replace("\\", "/")}
            for drive in drives
            if drive
        )
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

                    try:
                        entry_resolved = Path(entry.path).resolve()
                    except OSError, ValueError:
                        continue
                    if not self._is_allowed_path(Path(entry.path)) or not self._is_allowed_path(
                        entry_resolved
                    ):
                        continue
                    entry_full_path = str(entry_resolved).replace("\\", "/")
                    ext = Path(name).suffix.lower()
                    is_openapi = ext in {".json", ".yaml", ".yml"} and document_permitted(
                        entry_resolved
                    )
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

        git_metadata = {}
        if query_params.get("git") == ["1"]:
            try:
                repository = git(target_path, "rev-parse", "--show-toplevel").decode().strip()
                commit = git(target_path, "rev-parse", "HEAD").decode().strip()
                if self._is_allowed_path(Path(repository).resolve()):
                    git_metadata = {"repo_root": repository, "git_head": commit}
            except OSError, ValueError, TimeoutError:
                pass

        self._send_json(
            {
                "current_path": target_path.as_posix(),
                "parent_path": parent_path,
                "workspace_root": workspace_root.as_posix(),
                "filter": filter_mode,
                "quick_dirs": quick_dirs,
                "items": items,
                **git_metadata,
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

        if not self._check_origin() or not self._check_read_auth():
            return
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            if not 0 <= content_len <= 5_000_000:
                raise ValueError("JSON body exceeds request limit")
            body = self.rfile.read(content_len).decode("utf-8") if content_len else "{}"
            data = json.loads(body) if body.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
        except (UnicodeError, ValueError) as exc:
            self._workflow_error(exc)
            return

        if path == "/api/model/config":
            try:
                self._send_json(self.workflow.model_config(data))
            except Exception as exc:
                self._workflow_error(exc)
            return

        parts = path.split("/")
        if (
            len(parts) in {5, 6}
            and parts[1:3] == ["api", "tasks"]
            and parts[4]
            in {
                "environment",
                "cases",
                "generate",
                "review",
                "approve",
                "execute",
                "recover",
                "recover-design",
            }
        ):
            task_id, action = parts[3:5]
            try:
                validate_task_id(task_id)
                if len(parts) == 6 and action != "cases":
                    raise FileNotFoundError("Unknown workflow endpoint")
                payload = self.workflow.post(
                    task_id, action, data, parts[5] if len(parts) == 6 else None
                )
                status = HTTPStatus.ACCEPTED if action in {"generate", "execute"} else HTTPStatus.OK
                self._send_json(payload, status=status)
            except Exception as exc:
                self._workflow_error(exc)
            return

        # 1. Safe Ping (No paid model inference, zero key leak)
        if path == "/api/model/ping":
            base_url = str(data.get("base_url") or "").strip()
            key_env = str(data.get("api_key_env") or "").strip()

            if not re.fullmatch(r"https?://[\w.-]+(?::\d+)?(?:/[\w./-]*)?", base_url):
                self._send_error("Enter an HTTP(S) provider URL without credentials or query")
                return

            key_found = bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env) and read_env(key_env))
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
            task_id = str(data.get("task_id") or "").strip() or f"task-{uuid4().hex[:12]}"
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
                    source_directory=data.get("source_directory") or None,
                    adapter=data.get("adapter") or None,
                    document_paths=tuple(data.get("document_paths", [])),
                    openapi_url=data.get("openapi_url") or None,
                    openapi_options=data.get("openapi_options") or None,
                    source_paths=tuple(data.get("source_paths", [])),
                    environment=data.get("environment") or None,
                    version_manifest=data.get("version_manifest") or {},
                )
                self._validate_local_materials(task)
                self.store.create(task)
                self._send_json(task.model_dump(), status=HTTPStatus.CREATED)
            except Exception as exc:
                self._workflow_error(exc)
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
                    "source_directory",
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

                with self.workflow.mutation(task_id):
                    previous = self.store.load(task_id)
                    self._validate_local_materials(
                        Task.model_validate(previous.model_dump() | updates)
                    )
                    updated = self.store.configure(task_id, **updates)
                self._send_json(updated.model_dump())
            except Exception as exc:
                self._workflow_error(exc)
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
                with self.workflow.mutation(task_id):
                    self._validate_local_materials(self.store.load(task_id))
                    inventory_path = discover(self.store, task_id, materials_root=materials_root)
                inv_text = inventory_path.read_text("utf-8")
                self._send_text(inv_text, content_type="application/json; charset=utf-8")
            except Exception as exc:
                self._workflow_error(exc)
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
    allowed_roots: tuple[Path, ...] = (),  # noqa: ARG001 - compatibility for older launchers
) -> ThreadingHTTPServer:
    """Instantiate a configured ThreadingHTTPServer bound to the given root."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Web console must bind to loopback (127.0.0.1 or localhost)")
    store = TaskStore(root)
    token = auth_token or secrets.token_hex(16)
    workspace_root = Path.cwd().resolve()

    class CustomHandler(UIRequestHandler):
        pass

    CustomHandler.store = store
    CustomHandler.workflow = WebWorkflow(store)
    CustomHandler.static_dir = STATIC_DIR

    server = ThreadingHTTPServer((host, port), CustomHandler)
    server.auth_token = token
    server.workspace_root = workspace_root
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
        help=argparse.SUPPRESS,
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
