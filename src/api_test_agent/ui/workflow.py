"""Web orchestration for the existing task services and their stored artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
from pydantic import ValidationError

from api_test_agent.envvars import read_env
from api_test_agent.model import ModelError
from api_test_agent.settings import ModelSettings
from api_test_agent.tasks.approval import (
    ExecutionConfig,
    ReviewSubmission,
    approve,
    check_approval,
    current_cases,
    fingerprint,
    read_approval,
    validate_config,
)
from api_test_agent.tasks.design import (
    design,
    export_review,
    load_inventory,
    recover_design,
    validate_cases,
)
from api_test_agent.tasks.design_model import model_generator
from api_test_agent.tasks.execution import classify_accounting, execute_task
from api_test_agent.tasks.models import CaseRevision
from api_test_agent.tasks.report import generate_report
from api_test_agent.tasks.store import TaskStore, replace_local_file
from api_test_agent.ui.model_credentials import MAX_KEY_LENGTH, read_key, write_key


class BusyTask(ValueError):
    """Another mutation is in progress for the task."""


def now() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text("utf-8")) if path.is_file() else {}


def validate_model_key(key: Any) -> str | None:
    if key is not None and (
        not isinstance(key, str) or len(key) > MAX_KEY_LENGTH or not re.fullmatch(r"[!-~]+", key)
    ):
        raise ValueError("请输入不含空格的有效 API Key。")
    return key


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        replace_local_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_error(exc: Exception) -> str:
    if isinstance(exc, ModelError):
        return f"模型请求失败（{exc.kind}）"
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['type']}"
            for item in exc.errors(include_input=False, include_context=False, include_url=False)[
                :8
            ]
        )
    if isinstance(exc, (ValueError, OSError)):
        return str(exc)
    return f"操作失败（{type(exc).__name__}）"


def selected_ids(value: Any) -> list[str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("select a nonempty set of unique identifiers")
    return value


class WebWorkflow:
    def __init__(self, store: TaskStore):
        self.store = store
        self._lock = Lock()
        self._model_lock = Lock()
        self._busy: set[str] = set()
        self._live_jobs: set[str] = set()

    def _acquire(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._busy:
                raise BusyTask("该任务已有操作正在进行，请等待完成")
            self._busy.add(task_id)

    def _release(self, task_id: str) -> None:
        with self._lock:
            self._busy.remove(task_id)

    @contextmanager
    def mutation(self, task_id: str) -> Iterator[None]:
        self._acquire(task_id)
        try:
            yield
        finally:
            self._release(task_id)

    @property
    def _model_path(self) -> Path:
        return self.store.root / ".web-model.json"

    def _model(self) -> ModelSettings | None:
        raw = read_json(self._model_path)
        return ModelSettings.model_validate(raw) if raw else None

    def _credential_target(self, base_url: str) -> str:
        scope = f"{os.path.normcase(str(self.store.root))}\0{base_url.rstrip('/')}"
        return "TestPilot/WebModel/" + hashlib.sha256(scope.encode("utf-8")).hexdigest()

    def model_key_status(self, base_url: str, key_env: str) -> dict[str, bool]:
        stored = bool(read_key(self._credential_target(base_url)))
        return {
            "stored_key_configured": stored,
            "key_configured": stored
            or bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env) and read_env(key_env)),
        }

    def model_config(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._model_lock:
            if data is not None:
                fields = data.copy()
                key = validate_model_key(fields.pop("api_key", None))
                remove = fields.pop("remove_api_key", False)
                if type(remove) is not bool or (remove and key is not None):
                    raise ValueError("请单独保存或移除模型 API Key。")
                settings = ModelSettings.model_validate(fields)
                target = self._credential_target(settings.base_url)
                if key is not None or remove:
                    parts = urlsplit(settings.base_url)
                    if (
                        key is not None
                        and parts.scheme != "https"
                        and parts.hostname
                        not in {
                            "127.0.0.1",
                            "localhost",
                        }
                    ):
                        raise ValueError("保存 API Key 的远程模型地址必须使用 HTTPS。")
                    previous = read_key(target)
                    write_key(target, key)
                    try:
                        write_json(self._model_path, settings.model_dump(mode="json"))
                    except Exception:
                        write_key(target, previous)
                        raise
                else:
                    write_json(self._model_path, settings.model_dump(mode="json"))
            else:
                settings = self._model()
            if settings is None:
                return {"model": None, "key_configured": False, "stored_key_configured": False}
            return {
                "model": settings.model_dump(mode="json"),
                **self.model_key_status(
                    settings.base_url,
                    settings.api_key_env,
                ),
            }

    def list_models(self, data: dict[str, Any]) -> dict[str, list[str]]:
        key = validate_model_key(data.get("api_key"))
        if not isinstance(data.get("base_url"), str) or not data["base_url"]:
            raise ValueError("请先填写供应商 API 地址。")
        settings = ModelSettings.model_validate(
            {
                "base_url": data["base_url"],
                "model_id": "model-list",
                "api_key_env": data.get("api_key_env", "API_TEST_AGENT_MODEL_API_KEY"),
            }
        )
        parts = urlsplit(settings.base_url)
        local = parts.hostname in {"127.0.0.1", "localhost"}
        if parts.scheme != "https" and not local:
            raise ValueError("远程模型列表地址必须使用 HTTPS。")
        key = key or read_key(self._credential_target(settings.base_url))
        if not key and not local:
            saved = self._model()
            if (
                saved
                and saved.base_url.rstrip("/") == settings.base_url.rstrip("/")
                and saved.api_key_env == settings.api_key_env
            ):
                key = validate_model_key(read_env(saved.api_key_env))
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            with httpx2.Client(timeout=8, trust_env=not local, follow_redirects=False) as client:
                with client.stream(
                    "GET",
                    settings.base_url.rstrip("/") + "/models",
                    headers=headers,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise ValueError("供应商拒绝读取模型列表。请核对 API Key 或账号权限。")
                    if response.status_code in {404, 405}:
                        raise ValueError("该端点没有提供模型列表。请输入模型 ID。")
                    if response.status_code != 200:
                        raise ValueError(f"供应商模型列表返回 HTTP {response.status_code}。")
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 4_000_000:
                            raise ValueError("供应商模型列表过大。请输入模型 ID。")
        except httpx2.HTTPError, httpx2.InvalidURL:
            raise ValueError("无法读取供应商模型列表。请核对 API 地址或手动输入模型 ID。") from None
        try:
            decoded = json.loads(payload)
        except ValueError:
            raise ValueError("供应商未返回可识别的模型列表。请输入模型 ID。") from None
        rows = decoded.get("data") if isinstance(decoded, dict) else None
        if not isinstance(rows, list):
            raise ValueError("供应商未返回可识别的模型列表。请输入模型 ID。")
        ids = {
            model_id
            for row in rows
            if isinstance(row, dict)
            and isinstance(model_id := row.get("id"), str)
            and len(model_id) <= 256
            and re.fullmatch(r"[\w.:/-]+", model_id)
            and (not key or key not in model_id)
        }
        return {"models": sorted(ids)[:2000]}

    def _job(
        self,
        task_id: str,
        kind: str,
        work: Callable[[], dict[str, Any]],
        **details: Any,
    ) -> dict[str, Any]:
        """Caller owns the task mutation until the worker starts."""
        job_id = uuid4().hex
        path = self.store.folder(task_id) / "web-jobs" / f"{job_id}.json"
        job = {
            "job_id": job_id,
            "kind": kind,
            "status": "running",
            "started_at": now(),
            **details,
        }
        write_json(path, job)
        with self._lock:
            self._live_jobs.add(job_id)

        def run() -> None:
            try:
                job.update(work(), status="completed")
            except Exception as exc:
                job.update(status="failed", error=safe_error(exc))
            finally:
                try:
                    write_json(path, {**job, "finished_at": now()})
                finally:
                    with self._lock:
                        self._live_jobs.discard(job_id)
                    self._release(task_id)

        try:
            Thread(target=run, name=f"testpilot-{kind}-{job_id[:8]}", daemon=True).start()
        except RuntimeError:
            with self._lock:
                self._live_jobs.discard(job_id)
            write_json(path, {**job, "status": "failed", "error": "Could not start worker"})
            raise
        return dict(job)

    def jobs(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            live_jobs = self._live_jobs.copy()
            jobs = [
                read_json(path) for path in (self.store.folder(task_id) / "web-jobs").glob("*.json")
            ]
        for job in jobs:
            if job["status"] == "running" and job["job_id"] not in live_jobs:
                job.update(status="interrupted", error="控制台已停止，任务未记录完成")
        return sorted(jobs, key=lambda job: job["started_at"], reverse=True)

    def environment(self, task_id: str) -> ExecutionConfig | None:
        folder = self.store.folder(task_id)
        stored = read_json(folder / "web-environment.json")
        if stored:
            return ExecutionConfig.model_validate(stored)
        pointer = read_json(folder / "active-approval.json")
        if pointer:
            return read_approval(self.store, task_id, pointer["approval_id"]).execution_config
        return None

    def overview(self, task_id: str) -> dict[str, Any]:
        folder = self.store.folder(task_id)
        task = self.store.load(task_id)
        config = self.environment(task_id)
        pointer = read_json(folder / "active-approval.json")
        approval = None
        if pointer:
            approval_id = pointer["approval_id"]
            snapshot = read_approval(self.store, task_id, approval_id)
            stale = task.status not in {"ready", "running", "completed", "incomplete"}
            try:
                check_approval(self.store, task_id, approval_id, config, require_ready=False)
            except ValueError, OSError, PermissionError:
                stale = True
            approval = {
                "approval_id": approval_id,
                "cases": {case.case_id: case.fingerprint() for case in snapshot.cases},
                "stale": stale,
            }
        return {
            "environment": config.model_dump(mode="json") if config else None,
            "approval": approval,
            "case_hashes": {
                case_id: case.fingerprint()
                for case_id, case in current_cases(self.store, task_id).items()
            },
            "review_states": self.store.review_states(task_id),
            "jobs": self.jobs(task_id),
            "pending_imports": [
                marker.parent.name
                for marker in (folder / "designs").glob("*/import-started.json")
                if not (marker.parent / "import-completed.json").exists()
            ],
        }

    def _required_environment(self, task_id: str) -> ExecutionConfig:
        config = self.environment(task_id)
        if config is None:
            raise ValueError("请先保存执行环境")
        return config

    def _save_environment(self, task_id: str, data: dict[str, Any]) -> dict[str, Any]:
        task = self.store.load(task_id)
        config = ExecutionConfig.model_validate({"target_commit": task.target_commit, **data})
        validate_config(self.store, task_id, config)
        stored = config.model_dump(mode="json")
        write_json(self.store.folder(task_id) / "web-environment.json", stored)
        return stored

    def _revise(self, task_id: str, case_id: str, data: dict[str, Any]) -> dict[str, Any]:
        latest = current_cases(self.store, task_id).get(case_id)
        if latest is None:
            raise FileNotFoundError("用例不存在")
        if latest.fingerprint() != data.get("expected_hash"):
            raise BusyTask("此用例已有新修订，请刷新后再编辑")
        draft = data.get("case")
        if not isinstance(draft, dict):
            raise ValueError("缺少用例修订内容")
        case = CaseRevision.model_validate(
            {
                **draft,
                "case_id": case_id,
                "revision": latest.revision + 1,
                "origin": "human",
                "target_commit": latest.target_commit,
                "supersedes": latest.fingerprint(),
            }
        )
        validate_cases([case], load_inventory(self.store, task_id))
        self.store.add_case(task_id, case)
        return case.model_dump(mode="json")

    def _execute(self, task_id: str, data: dict[str, Any]) -> dict[str, Any]:
        task = self.store.load(task_id)
        config = self._required_environment(task_id)
        approval_id = data.get("approval_id")
        active = read_json(self.store.folder(task_id) / "active-approval.json")
        if (
            not isinstance(approval_id, str)
            or active.get("approval_id") != approval_id
            or task.status not in {"ready", "completed", "incomplete"}
        ):
            raise PermissionError("执行需要当前有效的人工审批")
        snapshot = read_approval(self.store, task_id, approval_id)
        check_approval(self.store, task_id, approval_id, config, require_ready=False)
        allowed = {case.case_id for case in snapshot.cases}
        case_ids = selected_ids(data.get("case_ids")) or sorted(allowed)
        if not set(case_ids) <= allowed:
            raise PermissionError("只能执行本次批准的用例")
        previous = data.get("previous_execution_id")
        if previous is not None:
            if not isinstance(previous, str) or not self._batch(task_id, previous):
                raise ValueError("找不到关联的历史批次")
        stop_on_failure = data.get("stop_on_failure", False)
        if not isinstance(stop_on_failure, bool):
            raise ValueError("stop_on_failure must be a boolean")
        execution_id = uuid4().hex

        def run() -> dict[str, Any]:
            accounting = execute_task(
                self.store,
                task_id,
                approval_id=approval_id,
                config=config,
                case_ids=case_ids,
                execution_id=execution_id,
                stop_on_failure=stop_on_failure,
            )
            generate_report(
                self.store,
                task_id,
                execution_id=execution_id,
                output_dir=self.store.folder(task_id) / "executions" / execution_id / "report",
                publish=True,
            )
            return {"outcome": classify_accounting(accounting)}

        return self._job(
            task_id,
            "execute",
            run,
            execution_id=execution_id,
            approval_id=approval_id,
            previous_execution_id=previous,
            case_ids=case_ids,
        )

    def post(self, task_id: str, action: str, data: dict[str, Any], item: str | None) -> Any:
        self.store.load(task_id)
        self._acquire(task_id)
        handed_off = False
        try:
            if action == "recover-design":
                attempt = data.get("design_id")
                if not isinstance(attempt, str):
                    raise ValueError("缺少待恢复的设计批次")
                return {"design_id": recover_design(self.store, task_id, attempt).name}
            if action == "recover":
                if data.get("acknowledge") is not True:
                    raise ValueError("请确认已检查中断批次；恢复不会自动重发任何请求")
                task = self.store.load(task_id)
                if task.status != "running":
                    raise ValueError("只有遗留的运行中任务需要恢复")
                pointer = read_json(self.store.folder(task_id) / "active-execution.json")
                execution_id = pointer.get("execution_id")
                batch = self._batch(task_id, execution_id)
                if not batch or batch.get("status") != "interrupted":
                    raise ValueError("没有可恢复的中断批次")
                folder = self.store.folder(task_id) / "executions" / execution_id
                summary = read_json(folder / "execution.json")
                summary.update(task_status="incomplete", interrupted=True, finished_at=now())
                write_json(folder / "execution.json", summary)
                self.store.transition(
                    task_id,
                    "incomplete",
                    "Human acknowledged interrupted run; no requests replayed",
                )
                generate_report(
                    self.store, task_id, execution_id=execution_id, output_dir=folder / "report"
                )
                return {"execution_id": execution_id, "status": "incomplete"}
            if action == "environment":
                return self._save_environment(task_id, data)
            if action == "cases" and item:
                return self._revise(task_id, item, data)
            if action in {"cases", "generate"}:
                operation_ids = selected_ids(data.get("operation_ids"))
                scope = set(operation_ids) if operation_ids else None
                if action == "cases":
                    response = data.get("response")
                    if not isinstance(response, str) or not response.strip():
                        raise ValueError("请提供用例设计 JSON")
                    attempt = design(
                        self.store, task_id, supplied_response=response, operation_ids=scope
                    )
                    return {"design_id": attempt.name}
                with self._model_lock:
                    settings = self._model()
                    if settings is None:
                        raise ValueError("请先保存模型配置")
                    key = read_key(self._credential_target(settings.base_url))
                    generator = (
                        model_generator(settings, api_key=key) if key else model_generator(settings)
                    )

                def generate() -> dict[str, Any]:
                    attempt = design(self.store, task_id, generator, operation_ids=scope)
                    return {"design_id": attempt.name}

                result = self._job(task_id, "generate", generate)
                handed_off = True
                return result
            if action == "review":
                folder = export_review(
                    self.store, task_id, self._required_environment(task_id)
                ).parent
                return {
                    "review_id": folder.name,
                    "review_hash": (folder / "manifest.sha256").read_text("ascii"),
                    "cases": read_json(folder / "manifest.json")["cases"],
                    "markdown": (folder / "review.md").read_text("utf-8"),
                    "rule_confirmations": [
                        {
                            "case_id": case.case_id,
                            "rule_id": rule.rule_id,
                            "statement": rule.statement,
                            "statement_hash": fingerprint(rule.statement),
                        }
                        for case in current_cases(self.store, task_id).values()
                        for rule in case.rules
                        if rule.basis == "hypothesis"
                    ],
                }
            if action == "approve":
                folder = approve(
                    self.store,
                    task_id,
                    ReviewSubmission.model_validate(data),
                    self._required_environment(task_id),
                )
                return {"approval_id": folder.name}
            if action == "execute":
                result = self._execute(task_id, data)
                handed_off = True
                return result
            raise FileNotFoundError("Unknown workflow endpoint")
        finally:
            if not handed_off:
                self._release(task_id)

    def batches(self, task_id: str) -> list[dict[str, Any]]:
        jobs = {job["execution_id"]: job for job in self.jobs(task_id) if job["kind"] == "execute"}
        batches = {execution_id: dict(job) for execution_id, job in jobs.items()}
        for folder in (self.store.folder(task_id) / "executions").glob("*"):
            summary = read_json(folder / "execution.json")
            if summary:
                job = jobs.get(folder.name, {})
                batches[folder.name] = {
                    "status": "completed",
                    **summary,
                    **job,
                    "execution_id": folder.name,
                }
        return sorted(
            batches.values(),
            key=lambda batch: batch.get("started_at") or batch.get("finished_at") or "",
            reverse=True,
        )

    def _batch(self, task_id: str, execution_id: str) -> dict[str, Any] | None:
        return next(
            (batch for batch in self.batches(task_id) if batch["execution_id"] == execution_id),
            None,
        )

    def batch(self, task_id: str, execution_id: str) -> dict[str, Any]:
        batch = self._batch(task_id, execution_id)
        if batch is None:
            raise FileNotFoundError("执行批次不存在")
        folder = self.store.folder(task_id) / "executions" / execution_id
        records = self.store.execution_records(task_id, execution_id)
        report = read_json(folder / "report" / "report.json")
        markdown_path = folder / "report" / "report.md"
        markdown = markdown_path.read_text("utf-8") if markdown_path.is_file() else ""
        if (
            not report
            and batch.get("status") == "completed"
            and (folder / "execution.json").is_file()
        ):
            # CLI executions predate the Web report cache.
            with self.mutation(task_id):
                _, markdown_path, json_path = generate_report(
                    self.store,
                    task_id,
                    execution_id=execution_id,
                    output_dir=folder / "report",
                    publish=False,
                )
                report = read_json(json_path)
                markdown = markdown_path.read_text("utf-8")
        return {
            "batch": batch,
            "records": [record.model_dump() for record in records],
            "markdown": markdown,
            "report": report,
        }
