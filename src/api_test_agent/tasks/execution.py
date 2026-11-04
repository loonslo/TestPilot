"""F5: Structured HTTP execution engine for frozen approved test cases.

Boundaries:
- Only executes frozen ApprovalSnapshot cases (from F4).
- Only talks to explicit 127.0.0.1 loopback URLs.
- Host tokens injected from process environment variables and REDACTED in all evidence.
- Full step-by-step event stream and immutable evidence records persisted.
- Preserves failure distinction: passed, failed, error, blocked, skipped, not_run.
- Respects interruption (max-cases, stop-on-failure); remaining cases marked not_run.
- Task status transitions to completed or incomplete with conservation verified.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from api_test_agent import expr
from api_test_agent.cases import lookup
from api_test_agent.tasks.approval import ExecutionConfig, check_approval, read_approval
from api_test_agent.tasks.models import (
    CaseRevision,
    EvidenceRecord,
    ExecutionAccounting,
    ExecutionRecord,
    Issue,
    Step,
)
from api_test_agent.tasks.store import TaskStore

SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "token"}


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = value
    return sanitized


def redact_sensitive(data: Any, secrets: set[str]) -> Any:
    if not secrets and not isinstance(data, (dict, list, tuple)):
        return data
    if isinstance(data, str):
        res = data
        for s in sorted(secrets, key=len, reverse=True):
            if s and s in res:
                res = res.replace(s, "[REDACTED]")
        return res
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if str(k).lower() in SENSITIVE_HEADERS:
                out[redact_sensitive(k, secrets)] = "[REDACTED]"
            else:
                out[redact_sensitive(k, secrets)] = redact_sensitive(v, secrets)
        return out
    if isinstance(data, list):
        return [redact_sensitive(item, secrets) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_sensitive(item, secrets) for item in data)
    return data


class RequestCounter:
    def __init__(self, limit: int):
        self.limit = limit
        self.count = 0

    def can_request(self) -> bool:
        return self.count < self.limit

    def increment(self) -> None:
        self.count += 1


def classify_accounting(accounting: ExecutionAccounting) -> str:
    """把结构化 accounting 映射成一次执行的结论，不从文本里猜结果。

    - empty       没有任何计划用例（例如 --max-cases 0），不是通过
    - failed      有断言失败
    - error       有执行异常、被阻塞的用例，或计数不守恒
    - incomplete  有未执行/跳过的用例：本轮没有覆盖全部计划
    - passed      计划内用例全部通过
    """

    if accounting.planned <= 0:
        return "empty"
    if accounting.failed > 0:
        return "failed"
    if accounting.error > 0 or accounting.blocked > 0 or not accounting.conserved:
        return "error"
    if accounting.not_run > 0 or accounting.skipped > 0:
        return "incomplete"
    if accounting.passed == accounting.planned:
        return "passed"
    return "incomplete"


def resolve_parameter_value(mode: str, value: Any, results: dict[str, dict[str, Any]]) -> Any:
    if mode == "omitted":
        return None
    if mode == "literal":
        return value
    if mode == "reference":
        if not isinstance(value, str):
            raise ValueError(f"reference must be placeholder string: {value!r}")
        match = re.fullmatch(r"\{\{([a-z][a-z0-9_]{0,23})\.([A-Za-z0-9_.-]+)\}\}", value)
        if not match:
            raise ValueError(f"malformed reference expression: {value!r}")
        return lookup(results, match.group(1), match.group(2))
    if mode == "generated":
        return value
    return value


def execute_step(
    client: httpx.Client,
    base_url: str,
    operation_dict: dict[str, Any],
    step: Step,
    results: dict[str, dict[str, Any]],
    tokens: dict[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
    counter: RequestCounter | None = None,
    secret_tokens: set[str] | None = None,
) -> tuple[dict[str, Any], EvidenceRecord]:
    secrets = secret_tokens or set()
    method = operation_dict["method"]
    raw_path = operation_dict["path"]

    path_params: dict[str, str] = {}
    query_params: dict[str, str] = {}
    headers: dict[str, str] = {}
    body_dict: dict[str, Any] = {}
    single_body: Any = None
    has_single_body = False

    started_at = time.monotonic()
    timestamp = datetime.now(UTC)

    # 1. Parameter resolution with safety wrap
    try:
        for param in step.parameters:
            if param.mode == "omitted":
                continue
            val = resolve_parameter_value(param.mode, param.value, results)
            if param.location == "path":
                path_params[param.name] = str(val)
            elif param.location == "query":
                query_params[param.name] = str(val)
            elif param.location == "header":
                headers[param.name] = str(val)
            elif param.location == "body":
                if param.name == "$":
                    has_single_body = True
                    single_body = val
                else:
                    body_dict[param.name] = val
    except Exception as exc:
        duration = round(time.monotonic() - started_at, 4)
        error_msg = f"Parameter resolution error: {type(exc).__name__}: {exc}"
        evidence_id = uuid4().hex
        evidence = EvidenceRecord(
            evidence_id=evidence_id,
            task_id=operation_dict.get("task_id", "unknown"),
            case_id=operation_dict.get("case_id", "unknown"),
            step_id=step.step_id,
            operation_id=step.operation_id,
            phase=step.phase,
            timestamp=timestamp,
            method=method,
            url=redact_sensitive(base_url.rstrip("/") + raw_path, secrets),
            request_parameters={"error": redact_sensitive(str(exc), secrets)},
            response_status=500,
            response_headers={},
            response_body=None,
            duration_seconds=duration,
            error=redact_sensitive(error_msg, secrets),
        )
        step_output = {
            "status": 0,
            "headers": {},
            "body": None,
            "data": None,
            "error": error_msg,
        }
        return step_output, evidence

    # Substitute path parameters
    resolved_path = raw_path
    for name, val_str in path_params.items():
        resolved_path = resolved_path.replace(f"{{{name}}}", val_str)

    # Inject host authentication
    if step.account != "anonymous":
        token = tokens.get(step.account)
        if token:
            headers["Authorization"] = f"Bearer {token}"

    full_url = base_url.rstrip("/") + resolved_path

    # Verify loopback
    parts = urlsplit(full_url)
    if parts.hostname != "127.0.0.1":
        raise ValueError(f"HTTP execution must target 127.0.0.1, got {parts.hostname}")

    body_payload = single_body if has_single_body else (body_dict if body_dict else None)
    has_body = has_single_body or bool(body_dict)

    request_record_params: dict[str, Any] = {
        "path_params": path_params,
        "query": query_params,
        "headers": sanitize_headers(headers),
        "body": body_payload,
        "body_provided": has_body,
    }

    # 2. Pre-call check: max_requests limit
    if counter is not None and not counter.can_request():
        duration = round(time.monotonic() - started_at, 4)
        error_msg = (
            f"Request limit exceeded: max_requests ({counter.limit}) "
            f"reached before sending step {step.step_id}"
        )
        evidence_id = uuid4().hex
        evidence = EvidenceRecord(
            evidence_id=evidence_id,
            task_id=operation_dict.get("task_id", "unknown"),
            case_id=operation_dict.get("case_id", "unknown"),
            step_id=step.step_id,
            operation_id=step.operation_id,
            phase=step.phase,
            timestamp=timestamp,
            method=method,
            url=redact_sensitive(full_url, secrets),
            request_parameters=redact_sensitive(request_record_params, secrets),
            response_status=500,
            response_headers={},
            response_body=None,
            duration_seconds=duration,
            error=redact_sensitive(error_msg, secrets),
        )
        step_output = {
            "status": 0,
            "headers": {},
            "body": None,
            "data": None,
            "error": error_msg,
        }
        return step_output, evidence

    error_msg = None
    resp_status = 500
    resp_headers: dict[str, str] = {}
    resp_body: Any = None

    interrupted: KeyboardInterrupt | None = None
    request_attempted = False
    response_received = False
    try:
        req_kwargs: dict[str, Any] = {
            "method": method,
            "url": full_url,
            "headers": headers,
            "params": query_params if query_params else None,
            "timeout": timeout_seconds,
        }
        if has_body and method in {"POST", "PUT", "PATCH"}:
            if body_payload is None:
                # httpx treats json=None as no body; encode explicit JSON null ourselves.
                req_kwargs["content"] = b"null"
                if not any(name.lower() == "content-type" for name in headers):
                    headers["Content-Type"] = "application/json"
            else:
                req_kwargs["json"] = body_payload

        if counter is not None:
            counter.increment()

        request_attempted = True
        response = client.request(**req_kwargs)
        response_received = True
        resp_status = response.status_code
        resp_headers = dict(response.headers)
        duration = round(time.monotonic() - started_at, 4)

        content_bytes = response.content
        if len(content_bytes) > max_response_bytes:
            content_bytes = content_bytes[:max_response_bytes]
            resp_body = f"[TRUNCATED {len(response.content)} bytes]"
        else:
            try:
                resp_body = response.json()
            except Exception:
                resp_body = response.text

    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            interrupted = exc
        duration = round(time.monotonic() - started_at, 4)
        error_msg = f"{type(exc).__name__}: {exc}"
        resp_status = 0

    evidence_id = uuid4().hex
    evidence = EvidenceRecord(
        evidence_id=evidence_id,
        task_id=operation_dict.get("task_id", "unknown"),
        case_id=operation_dict.get("case_id", "unknown"),
        step_id=step.step_id,
        operation_id=step.operation_id,
        phase=step.phase,
        timestamp=timestamp,
        method=method,
        url=redact_sensitive(full_url, secrets),
        request_parameters=redact_sensitive(request_record_params, secrets),
        request_attempted=request_attempted,
        response_received=response_received,
        response_status=max(100, min(599, resp_status)) if resp_status > 0 else 500,
        response_headers=redact_sensitive(sanitize_headers(resp_headers), secrets),
        response_body=redact_sensitive(resp_body, secrets),
        duration_seconds=duration,
        error=redact_sensitive(error_msg, secrets) if error_msg else None,
    )

    step_output = {
        "_interrupted": interrupted,
        "status": resp_status,
        "headers": resp_headers,
        "body": resp_body,
        "data": resp_body,
        "error": error_msg,
    }

    return step_output, evidence


def execute_task(
    store: TaskStore,
    task_id: str,
    *,
    approval_id: str | None = None,
    config: ExecutionConfig | None = None,
    max_cases: int | None = None,
    stop_on_failure: bool = False,
    http_client: httpx.Client | None = None,
    case_id: str | None = None,
) -> ExecutionAccounting:
    task = store.load(task_id)

    if task.status == "running":
        raise ValueError(
            f"task {task_id!r} is already in 'running' state; "
            "an execution may be in progress or crashed uncleanly. "
            "Inspect the interrupted run before transitioning to 'incomplete'."
        )

    if task.status not in {"ready", "incomplete", "completed"}:
        raise ValueError(f"task in status {task.status!r} cannot be executed")

    # Locate approval
    if approval_id is None:
        pointer_file = store.folder(task_id) / "active-approval.json"
        if not pointer_file.is_file():
            raise ValueError("task has no active approved execution selection")
        approval_id = json.loads(pointer_file.read_text("utf-8"))["approval_id"]

    snapshot = read_approval(store, task_id, approval_id)
    exec_config = config or snapshot.execution_config

    # Validate approval
    check_approval(store, task_id, approval_id, exec_config, require_ready=False)

    # Environment Identity Guard
    lowered_env = exec_config.environment.lower()
    if "prod" in lowered_env or lowered_env in {"production", "online"}:
        msg = f"production environment {exec_config.environment!r} is strictly forbidden"
        raise ValueError(f"Execution guard rejected: {msg}")
    if task.environment and exec_config.environment != task.environment:
        raise ValueError(
            f"Execution guard rejected: configuration environment {exec_config.environment!r} "
            f"does not match task environment {task.environment!r}"
        )

    # Set up tokens from environment
    tokens: dict[str, str] = {}
    for alias, env_name in exec_config.accounts.items():
        val = os.environ.get(env_name)
        if val:
            tokens[alias] = val

    # Resolve the requested scope before touching any state: an unknown case ID must not
    # leave the task in 'running'.
    target_cases = list(snapshot.cases)
    if case_id is not None:
        target_cases = [c for c in snapshot.cases if c.case_id == case_id]
        if not target_cases:
            raise ValueError(f"Case {case_id!r} not found in approved cases for task {task_id!r}")
    case_scoped = case_id is not None

    secret_tokens = {v for v in tokens.values() if isinstance(v, str) and v}

    client = http_client or httpx.Client(timeout=exec_config.timeout_seconds, trust_env=False)
    should_close_client = http_client is None

    # Running service version probe. It runs before the state transition so a failed probe
    # raises while the task is still in a clean state.
    is_version_verified = False
    actual_commit = "unverified"
    if exec_config.version_endpoint:
        probe_url = f"{exec_config.base_url.rstrip('/')}/{exec_config.version_endpoint.lstrip('/')}"
        try:
            probe_resp = client.get(probe_url, timeout=min(5.0, exec_config.timeout_seconds))
            if probe_resp.status_code == 200:
                try:
                    probe_data = probe_resp.json()
                except Exception as json_err:
                    if exec_config.require_version_verification:
                        raise ValueError(
                            f"Version probe response at {probe_url} is not valid JSON: {json_err}"
                        ) from json_err
                    probe_data = None

                if isinstance(probe_data, dict):
                    probed_val = (
                        probe_data.get("commit")
                        or probe_data.get("git_sha")
                        or probe_data.get("version")
                        or probe_data.get("sha")
                    )
                    if probed_val:
                        actual_commit = str(probed_val)
                        if actual_commit == exec_config.target_commit:
                            is_version_verified = True
                        elif exec_config.require_version_verification:
                            raise ValueError(
                                "Version verification failed: running service probed commit "
                                f"{actual_commit!r} does not match expected target_commit "
                                f"{exec_config.target_commit!r}"
                            )
            elif exec_config.require_version_verification:
                raise ValueError(
                    f"Version probe at {probe_url} returned HTTP {probe_resp.status_code}"
                )
        except Exception as probe_err:
            if should_close_client:
                client.close()
            if exec_config.require_version_verification:
                raise
            actual_commit = f"probe_failed: {probe_err}"

    # Read (and validate) the prior run before creating anything: a broken pointer must not
    # leave a half-built execution directory behind.
    resume_from: tuple[str, int] | None = None
    if task.status == "incomplete" and not case_scoped:
        active_exec_file = store.folder(task_id) / "active-execution.json"
        if not active_exec_file.is_file():
            raise ValueError("Cannot safely resume: prior execution pointer is missing")
        prev_info = json.loads(active_exec_file.read_text("utf-8"))
        if prev_info.get("approval_id") == approval_id and prev_info.get("execution_id"):
            prev_exec_id = prev_info["execution_id"]
            previous_folder = store.folder(task_id) / "executions" / prev_exec_id
            summary = json.loads((previous_folder / "execution.json").read_text("utf-8"))
            used = summary.get("requests_attempted")
            if not isinstance(used, int) or isinstance(used, bool) or used < 0:
                raise ValueError("Cannot safely resume: prior request count is unknown")
            resume_from = (prev_exec_id, used)

    # Setup directories
    execution_id = uuid4().hex
    exec_folder = store.folder(task_id) / "executions" / execution_id
    exec_folder.mkdir(parents=True, exist_ok=False)

    events_file = exec_folder / "events.jsonl"
    records_folder = exec_folder / "records"
    records_folder.mkdir(exist_ok=True)
    evidence_folder = exec_folder / "evidence"
    evidence_folder.mkdir(exist_ok=True)

    is_resuming = False
    counts = {
        "passed": 0,
        "failed": 0,
        "error": 0,
        "blocked": 0,
        "skipped": 0,
        "not_run": 0,
    }
    issues_list: list[Issue] = []
    executed_count = 0
    halt_execution = False
    halt_reason: str | None = None
    counter = RequestCounter(exec_config.max_requests)
    recorded_case_hashes: set[str] = set()

    if resume_from is not None:
        prev_exec_id, used_requests = resume_from
        counter.count = used_requests
        for issue in store.issues(task_id, prev_exec_id):
            store.save_issue(task_id, execution_id, issue)
        for r in store.execution_records(task_id, prev_exec_id):
            if r.outcome in {"passed", "failed", "error", "blocked", "skipped"}:
                is_resuming = True
                store.add_execution_record(task_id, execution_id, r)
                for ev_id in r.evidence_ids:
                    ev = store.load_evidence(task_id, ev_id, prev_exec_id)
                    if ev:
                        store.save_evidence(task_id, execution_id, ev)
                counts[r.outcome] += 1
                executed_count += 1
                recorded_case_hashes.add(r.revision_hash)

    # Point to active execution
    (store.folder(task_id) / "active-execution.json").write_text(
        json.dumps({"execution_id": execution_id, "approval_id": approval_id}),
        encoding="utf-8",
    )

    store.transition(
        task_id,
        "running",
        f"{'Resuming' if is_resuming else 'Starting'} execution run {execution_id}",
    )

    def log_event(kind: str, **payload: Any) -> None:
        sanitized_payload = {k: redact_sensitive(v, secret_tokens) for k, v in payload.items()}
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            **sanitized_payload,
        }
        with events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    log_event(
        "execution_started",
        execution_id=execution_id,
        approval_id=approval_id,
        resumed=is_resuming,
    )

    operations_map = {op.operation_id: op for op in snapshot.inventory.operations}

    current_case: CaseRevision | None = None
    case_evidence_ids: list[str] = []
    interrupted_exc: BaseException | None = None
    current_case_request_count = counter.count

    try:
        for case in target_cases:
            if case.fingerprint() in recorded_case_hashes:
                # Already executed in this or prior session, avoid repeating side effects
                continue

            current_case = case
            current_case_request_count = counter.count
            case_evidence_ids = []

            # Check max_cases interruption
            if max_cases is not None and executed_count >= max_cases:
                halt_execution = True
                halt_reason = f"max_cases limit ({max_cases}) reached"

            # Check if request limit was reached
            if not counter.can_request():
                halt_execution = True
                halt_reason = f"max_requests limit ({counter.limit}) reached"

            if halt_execution:
                rec = ExecutionRecord(
                    task_id=task_id,
                    revision_hash=case.fingerprint(),
                    attempt=1,
                    outcome="not_run",
                    evidence_ids=(),
                    reason=halt_reason or "Execution stopped before reaching this case",
                )
                store.add_execution_record(task_id, execution_id, rec)
                counts["not_run"] += 1
                recorded_case_hashes.add(case.fingerprint())
                log_event("case_not_run", case_id=case.case_id, reason=rec.reason)
                continue

            # Check execution_kind guard
            if case.execution_kind not in {"http", "structured-http-v1"}:
                rec = ExecutionRecord(
                    task_id=task_id,
                    revision_hash=case.fingerprint(),
                    attempt=1,
                    outcome="blocked",
                    evidence_ids=(),
                    reason=(
                        f"Execution guard rejected: unsupported execution_kind "
                        f"'{case.execution_kind}'; runner only supports 'structured-http-v1'"
                    ),
                )
                store.add_execution_record(task_id, execution_id, rec)
                counts["blocked"] += 1
                recorded_case_hashes.add(case.fingerprint())
                issue = Issue(
                    issue_id=f"ISSUE-{uuid4().hex[:8].upper()}",
                    task_id=task_id,
                    category="environment_issue",
                    title=f"Unsupported execution_kind for {case.case_id}",
                    detail=rec.reason,
                    case_id=case.case_id,
                )
                issues_list.append(issue)
                store.save_issue(task_id, execution_id, issue)
                log_event("case_blocked", case_id=case.case_id, reason=rec.reason)
                executed_count += 1
                continue

            # Check accounts precondition
            missing_tokens = []
            for step in case.steps:
                if step.account != "anonymous" and step.account not in tokens:
                    missing_tokens.append(
                        f"{step.account} (${exec_config.accounts.get(step.account)})"
                    )
            if missing_tokens:
                creds_desc = ", ".join(missing_tokens)
                rec = ExecutionRecord(
                    task_id=task_id,
                    revision_hash=case.fingerprint(),
                    attempt=1,
                    outcome="blocked",
                    evidence_ids=(),
                    reason=f"Preconditions not met: missing host credentials for {creds_desc}",
                )
                store.add_execution_record(task_id, execution_id, rec)
                counts["blocked"] += 1
                recorded_case_hashes.add(case.fingerprint())
                issue = Issue(
                    issue_id=f"ISSUE-{uuid4().hex[:8].upper()}",
                    task_id=task_id,
                    category="environment_issue",
                    title=f"Missing credential for {case.case_id}",
                    detail=rec.reason,
                    case_id=case.case_id,
                )
                issues_list.append(issue)
                store.save_issue(task_id, execution_id, issue)
                log_event("case_blocked", case_id=case.case_id, reason=rec.reason)
                executed_count += 1
                continue

            # Execute case
            log_event("case_started", case_id=case.case_id, revision_hash=case.fingerprint())
            results: dict[str, dict[str, Any]] = {}
            case_outcome = "passed"
            case_reason = "Test steps and assertions completed successfully"
            aborted_case = False

            prepare_steps = [s for s in case.steps if s.phase == "prepare"]
            test_steps = [s for s in case.steps if s.phase == "test"]
            verify_steps = [s for s in case.steps if s.phase == "verify"]
            cleanup_steps = [s for s in case.steps if s.phase == "cleanup"]

            # 1. Prepare phase
            for step in prepare_steps:
                log_event(
                    "step_started", case_id=case.case_id, step_id=step.step_id, phase="prepare"
                )
                op_data = operations_map[step.operation_id].model_dump()
                op_data.update(task_id=task_id, case_id=case.case_id)
                step_out, ev = execute_step(
                    client,
                    exec_config.base_url,
                    op_data,
                    step,
                    results,
                    tokens,
                    exec_config.timeout_seconds,
                    exec_config.max_response_bytes,
                    counter=counter,
                    secret_tokens=secret_tokens,
                )
                store.save_evidence(task_id, execution_id, ev)
                case_evidence_ids.append(ev.evidence_id)
                if step_out.get("_interrupted") is not None:
                    raise step_out["_interrupted"]

                results[step.step_id] = step_out
                log_event(
                    "step_finished",
                    case_id=case.case_id,
                    step_id=step.step_id,
                    status=step_out["status"],
                    evidence_id=ev.evidence_id,
                )
                if ev.error or step_out["status"] >= 400 or step_out["status"] == 0:
                    case_outcome = "error"
                    err_info = ev.error or "error"
                    case_reason = (
                        f"Prepare step {step.step_id} failed with "
                        f"status {step_out['status']} ({err_info})"
                    )
                    issue = Issue(
                        issue_id=f"ISSUE-{uuid4().hex[:8].upper()}",
                        task_id=task_id,
                        category="environment_issue",
                        title=redact_sensitive(f"Prepare failure in {case.case_id}", secret_tokens),
                        detail=redact_sensitive(case_reason, secret_tokens),
                        case_id=case.case_id,
                        step_id=step.step_id,
                        evidence_ids=tuple(case_evidence_ids),
                    )
                    issues_list.append(issue)
                    store.save_issue(task_id, execution_id, issue)
                    aborted_case = True
                    if not counter.can_request():
                        halt_execution = True
                        halt_reason = f"max_requests limit ({counter.limit}) reached"
                    break

            # 2. Test phase
            if not aborted_case:
                for step in test_steps:
                    log_event(
                        "step_started", case_id=case.case_id, step_id=step.step_id, phase="test"
                    )
                    op_data = operations_map[step.operation_id].model_dump()
                    op_data.update(task_id=task_id, case_id=case.case_id)
                    step_out, ev = execute_step(
                        client,
                        exec_config.base_url,
                        op_data,
                        step,
                        results,
                        tokens,
                        exec_config.timeout_seconds,
                        exec_config.max_response_bytes,
                        counter=counter,
                        secret_tokens=secret_tokens,
                    )
                    store.save_evidence(task_id, execution_id, ev)
                    case_evidence_ids.append(ev.evidence_id)
                    if step_out.get("_interrupted") is not None:
                        raise step_out["_interrupted"]

                    results[step.step_id] = step_out
                    log_event(
                        "step_finished",
                        case_id=case.case_id,
                        step_id=step.step_id,
                        status=step_out["status"],
                        evidence_id=ev.evidence_id,
                    )
                    if ev.error or step_out["status"] == 0:
                        case_outcome = "error"
                        err_info = ev.error or f"status {step_out['status']}"
                        case_reason = f"Test step {step.step_id} execution error: {err_info}"
                        issue = Issue(
                            issue_id=f"ISSUE-{uuid4().hex[:8].upper()}",
                            task_id=task_id,
                            category="environment_issue",
                            title=redact_sensitive(
                                f"HTTP execution error in {case.case_id}", secret_tokens
                            ),
                            detail=redact_sensitive(case_reason, secret_tokens),
                            case_id=case.case_id,
                            step_id=step.step_id,
                            evidence_ids=tuple(case_evidence_ids),
                        )
                        issues_list.append(issue)
                        store.save_issue(task_id, execution_id, issue)
                        aborted_case = True
                        if not counter.can_request():
                            halt_execution = True
                            halt_reason = f"max_requests limit ({counter.limit}) reached"
                        break

            # 3. Verify phase
            if not aborted_case:
                for step in verify_steps:
                    log_event(
                        "step_started", case_id=case.case_id, step_id=step.step_id, phase="verify"
                    )
                    op_data = operations_map[step.operation_id].model_dump()
                    op_data.update(task_id=task_id, case_id=case.case_id)
                    step_out, ev = execute_step(
                        client,
                        exec_config.base_url,
                        op_data,
                        step,
                        results,
                        tokens,
                        exec_config.timeout_seconds,
                        exec_config.max_response_bytes,
                        counter=counter,
                        secret_tokens=secret_tokens,
                    )
                    store.save_evidence(task_id, execution_id, ev)
                    case_evidence_ids.append(ev.evidence_id)
                    if step_out.get("_interrupted") is not None:
                        raise step_out["_interrupted"]

                    results[step.step_id] = step_out
                    log_event(
                        "step_finished",
                        case_id=case.case_id,
                        step_id=step.step_id,
                        status=step_out["status"],
                        evidence_id=ev.evidence_id,
                    )
                    if ev.error or step_out["status"] == 0:
                        case_outcome = "error"
                        err_info = ev.error or f"status {step_out['status']}"
                        case_reason = f"Verify step {step.step_id} execution error: {err_info}"
                        aborted_case = True
                        if not counter.can_request():
                            halt_execution = True
                            halt_reason = f"max_requests limit ({counter.limit}) reached"
                        break

            # 4. Assertions evaluation
            if not aborted_case:
                active_res = results

                def lookup_fn(s_id: str, path: str, res: dict = active_res) -> Any:
                    return lookup(res, s_id, path)

                for assertion in case.assertions:
                    try:
                        actual = expr.evaluate(assertion.expression, lookup_fn)
                        passed = actual == assertion.expected
                    except Exception as exc:
                        actual = None
                        passed = False
                        case_outcome = "error"
                        case_reason = (
                            f"Assertion evaluation error in {assertion.expression!r}: {exc}"
                        )
                        issue = Issue(
                            issue_id=f"ISSUE-{uuid4().hex[:8].upper()}",
                            task_id=task_id,
                            category="case_issue",
                            title=redact_sensitive(
                                f"Assertion expression error in {case.case_id}", secret_tokens
                            ),
                            detail=redact_sensitive(case_reason, secret_tokens),
                            case_id=case.case_id,
                            rule_ids=assertion.rule_ids,
                            evidence_ids=tuple(case_evidence_ids),
                        )
                        issues_list.append(issue)
                        store.save_issue(task_id, execution_id, issue)
                        break

                    log_event(
                        "assertion_evaluated",
                        case_id=case.case_id,
                        expression=assertion.expression,
                        expected=assertion.expected,
                        actual=actual,
                        passed=passed,
                    )

                    if not passed:
                        case_outcome = "failed"
                        case_reason = (
                            f"Assertion failed: {assertion.expression} evaluated to {actual!r}, "
                            f"expected {assertion.expected!r}"
                        )
                        matching_rules = [r for r in case.rules if r.rule_id in assertion.rule_ids]
                        if any(r.basis == "hypothesis" for r in matching_rules):
                            issue_cat = "unconfirmed_hypothesis"
                        else:
                            issue_cat = "suspected_defect"

                        issue = Issue(
                            issue_id=f"ISSUE-{uuid4().hex[:8].upper()}",
                            task_id=task_id,
                            category=issue_cat,
                            title=redact_sensitive(
                                f"Assertion failed: {assertion.expression}", secret_tokens
                            ),
                            detail=redact_sensitive(case_reason, secret_tokens),
                            case_id=case.case_id,
                            rule_ids=assertion.rule_ids,
                            expected=redact_sensitive(assertion.expected, secret_tokens),
                            actual=redact_sensitive(actual, secret_tokens),
                            evidence_ids=tuple(case_evidence_ids),
                        )
                        issues_list.append(issue)
                        store.save_issue(task_id, execution_id, issue)
                        break

            # 5. Cleanup phase
            cleanup_failed = False
            for step in cleanup_steps:
                log_event(
                    "step_started", case_id=case.case_id, step_id=step.step_id, phase="cleanup"
                )
                op_data = operations_map[step.operation_id].model_dump()
                op_data.update(task_id=task_id, case_id=case.case_id)
                step_out, ev = execute_step(
                    client,
                    exec_config.base_url,
                    op_data,
                    step,
                    results,
                    tokens,
                    exec_config.timeout_seconds,
                    exec_config.max_response_bytes,
                    counter=counter,
                    secret_tokens=secret_tokens,
                )
                store.save_evidence(task_id, execution_id, ev)
                case_evidence_ids.append(ev.evidence_id)
                if step_out.get("_interrupted") is not None:
                    raise step_out["_interrupted"]

                results[step.step_id] = step_out
                log_event(
                    "step_finished",
                    case_id=case.case_id,
                    step_id=step.step_id,
                    status=step_out["status"],
                    evidence_id=ev.evidence_id,
                )
                if ev.error or step_out["status"] >= 400 or step_out["status"] == 0:
                    cleanup_failed = True
                    log_event("cleanup_warning", case_id=case.case_id, step_id=step.step_id)
                    cleanup_detail = (
                        f"Cleanup step {step.step_id} failed with status {step_out['status']}"
                    )
                    issue = Issue(
                        issue_id=f"ISSUE-{uuid4().hex[:8].upper()}",
                        task_id=task_id,
                        category="cleanup_failure",
                        title=redact_sensitive(
                            f"Cleanup step failed in {case.case_id}", secret_tokens
                        ),
                        detail=redact_sensitive(cleanup_detail, secret_tokens),
                        case_id=case.case_id,
                        step_id=step.step_id,
                        evidence_ids=(ev.evidence_id,),
                    )
                    issues_list.append(issue)
                    store.save_issue(task_id, execution_id, issue)

            if cleanup_failed and case_outcome == "passed":
                case_reason = f"{case_reason} (Warning: cleanup step encountered failures)"

            rec = ExecutionRecord(
                task_id=task_id,
                revision_hash=case.fingerprint(),
                attempt=1,
                outcome=case_outcome,  # type: ignore[arg-type]
                evidence_ids=tuple(case_evidence_ids),
                reason=redact_sensitive(case_reason, secret_tokens),
            )
            store.add_execution_record(task_id, execution_id, rec)
            counts[case_outcome] += 1
            executed_count += 1
            recorded_case_hashes.add(case.fingerprint())
            log_event(
                "case_finished", case_id=case.case_id, outcome=case_outcome, reason=rec.reason
            )

            if stop_on_failure and case_outcome in {"failed", "error"}:
                halt_execution = True
                halt_reason = f"stop_on_failure triggered by {case.case_id} ({case_outcome})"

    except (KeyboardInterrupt, Exception) as exc:
        interrupted_exc = exc
        log_event("execution_interrupted", error=f"{type(exc).__name__}: {str(exc)}")
        if current_case and current_case.fingerprint() not in recorded_case_hashes:
            err_reason = f"Interrupted: {type(exc).__name__}: {str(exc)}"
            if case_evidence_ids or counter.count > current_case_request_count:
                rec = ExecutionRecord(
                    task_id=task_id,
                    revision_hash=current_case.fingerprint(),
                    attempt=1,
                    outcome="error",
                    evidence_ids=tuple(case_evidence_ids),
                    reason=redact_sensitive(err_reason, secret_tokens),
                )
                counts["error"] += 1
            else:
                rec = ExecutionRecord(
                    task_id=task_id,
                    revision_hash=current_case.fingerprint(),
                    attempt=1,
                    outcome="not_run",
                    evidence_ids=(),
                    reason=redact_sensitive(err_reason, secret_tokens),
                )
                counts["not_run"] += 1
            store.add_execution_record(task_id, execution_id, rec)
            executed_count += 1
            recorded_case_hashes.add(current_case.fingerprint())
            log_event(
                "case_finished",
                case_id=current_case.case_id,
                outcome=rec.outcome,
                reason=rec.reason,
            )

        # Mark all remaining unexecuted cases as not_run
        for rem_case in target_cases:
            if rem_case.fingerprint() not in recorded_case_hashes:
                rec = ExecutionRecord(
                    task_id=task_id,
                    revision_hash=rem_case.fingerprint(),
                    attempt=1,
                    outcome="not_run",
                    evidence_ids=(),
                    reason=f"Execution interrupted before reaching this case: {type(exc).__name__}",
                )
                store.add_execution_record(task_id, execution_id, rec)
                counts["not_run"] += 1
                recorded_case_hashes.add(rem_case.fingerprint())
                log_event("case_not_run", case_id=rem_case.case_id)

    finally:
        planned_total = len(target_cases)
        evidence_backed_count = 0
        missing_evidence_ids: list[str] = []
        missing_evidence_count = 0
        for record in store.execution_records(task_id, execution_id):
            if record.outcome not in {"passed", "failed", "error"}:
                continue
            missing = []
            for evidence_id in record.evidence_ids:
                try:
                    evidence = store.load_evidence(task_id, evidence_id, execution_id)
                except ValueError, OSError:
                    evidence = None
                if evidence is None:
                    missing.append(evidence_id)
            if not record.evidence_ids:
                missing.append(f"missing_evidence_for_{record.revision_hash[:16]}")
            if missing:
                missing_evidence_count += 1
                missing_evidence_ids.extend(missing)
                counts[record.outcome] -= 1
            else:
                evidence_backed_count += 1
        accounting = ExecutionAccounting(
            planned=planned_total,
            passed=counts["passed"],
            failed=counts["failed"],
            error=counts["error"],
            blocked=counts["blocked"],
            skipped=counts["skipped"],
            not_run=counts["not_run"],
            conserved=(
                missing_evidence_count == 0
                and planned_total
                == counts["passed"]
                + counts["failed"]
                + counts["error"]
                + counts["blocked"]
                + counts["skipped"]
                + counts["not_run"]
            ),
            evidence_backed_count=evidence_backed_count,
            missing_evidence_count=missing_evidence_count,
            missing_evidence_ids=tuple(missing_evidence_ids),
        )

        # Save summary metadata
        summary = {
            "execution_id": execution_id,
            "task_id": task_id,
            "approval_id": approval_id,
            "accounting": accounting.model_dump(),
            "finished_at": datetime.now(UTC).isoformat(),
            "cases_count": planned_total,
            "approved_cases_count": len(snapshot.cases),
            "scope": "case" if case_scoped else "approved_selection",
            "case_id": case_id,
            "requests_attempted": counter.count,
            "declared_commit": exec_config.target_commit,
            "actual_commit": actual_commit,
            "version_verified": is_version_verified,
        }
        (exec_folder / "execution.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        # Transition task state. A run that only covered part of the approved selection is not
        # a completed task: it goes back to 'ready' so the remaining cases stay executable.
        if accounting.not_run > 0 or interrupted_exc is not None or not accounting.conserved:
            store.transition(
                task_id,
                "incomplete",
                (
                    f"Execution stopped with {accounting.not_run} "
                    f"unexecuted cases out of {planned_total}"
                ),
            )
        elif case_scoped:
            store.transition(
                task_id,
                "ready",
                (
                    f"Case-scoped execution finished for {case_id!r} "
                    f"({len(snapshot.cases) - 1} other approved cases remain executable)"
                ),
            )
        else:
            store.transition(
                task_id,
                "completed",
                f"Execution completed for all {planned_total} approved cases",
            )

        log_event("execution_finished", accounting=accounting.model_dump())
        if should_close_client:
            client.close()

    if isinstance(interrupted_exc, KeyboardInterrupt):
        raise interrupted_exc

    return accounting
