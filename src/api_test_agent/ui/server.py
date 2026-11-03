"""Lightweight built-in Web console server for TestPilot.

Zero-dependency HTTP server delivering:
- Interactive dashboard UI (HTML/Tailwind/CSS tokens following baoyu-design)
- Real-time pipeline state (Task -> Inventory -> Cases -> Execution -> Report)
- Complete process artifacts inspection (Sources drawer)
- Safe offline model connectivity checks
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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


class UIRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the TestPilot Web Console."""

    store: TaskStore
    static_dir: Path = STATIC_DIR

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep console output concise and clean
        sys.stderr.write(f"[TestPilot UI] {self.address_string()} - {format % args}\n")

    def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(
        self,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
        status: int = HTTPStatus.OK,
    ) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status=status)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: C901, PLR0912
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        # 1. Static Assets
        if path in {"/", "/dashboard.html", "/index.html"}:
            html_file = self.static_dir / "dashboard.html"
            if not html_file.is_file():
                self._send_error("Dashboard template not found", status=HTTPStatus.NOT_FOUND)
                return
            self._send_text(html_file.read_text("utf-8"), content_type="text/html; charset=utf-8")
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
            if not task_id:
                self._send_error("task_id is required")
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
                allowed_keys = {
                    "target_project",
                    "target_commit",
                    "scope",
                    "repository",
                    "source_paths",
                    "document_paths",
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
                inventory_path = discover(self.store, task_id)
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
) -> ThreadingHTTPServer:
    """Instantiate a configured ThreadingHTTPServer bound to the given root."""
    store = TaskStore(root)

    class CustomHandler(UIRequestHandler):
        pass

    CustomHandler.store = store
    CustomHandler.static_dir = STATIC_DIR

    server = ThreadingHTTPServer((host, port), CustomHandler)
    return server


def run_server(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
) -> None:
    """Run the Web console server loop."""
    server = make_server(root, host, port)
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
    parser.set_defaults(ui_handler=execute_ui_command)


def execute_ui_command(args: argparse.Namespace) -> int:
    """Execute handler for `testpilot ui`."""
    args.root.mkdir(parents=True, exist_ok=True)
    run_server(
        root=args.root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
    return 0
