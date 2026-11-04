"""End-to-end verified real closed-loop test from empty test set to execution and report.

Tests the full lifecycle:
1. Empty task creation (task init)
2. Interface discovery via OpenAPI adapter (inventory.json)
3. Case design with parameter rationales (cases.json)
4. Human review materials export (review.md & manifest.json)
5. Review decision & frozen approval snapshot (approval.json)
6. Controlled execution against a real running loopback HTTP server
7. Real HTTP responses, step assertions, parameter references, and evidence generation
8. Report generation with strict conservation accounting and gap verification
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from api_test_agent.tasks.approval import (
    CaseDecision,
    ExecutionConfig,
    ReviewSubmission,
    approve,
    fingerprint,
)
from api_test_agent.tasks.design import design, export_review
from api_test_agent.tasks.discovery import discover
from api_test_agent.tasks.execution import execute_task
from api_test_agent.tasks.models import Task
from api_test_agent.tasks.report import generate_report
from api_test_agent.tasks.store import TaskStore


class MiniBusinessServiceHandler(BaseHTTPRequestHandler):
    """Mini business HTTP service for real end-to-end loopback execution."""

    accounts_db: dict[str, dict] = {}

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # Quiet test output

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            return

        if self.path.startswith("/api/v1/accounts/"):
            acc_id = self.path.split("/")[-1]
            if acc_id in self.accounts_db:
                payload = json.dumps(self.accounts_db[acc_id]).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Host-Token-Echo", self.headers.get("Authorization", ""))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/v1/accounts":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            acc_id = f"acc-{len(self.accounts_db) + 1}"
            account = {
                "account_id": acc_id,
                "name": body.get("name", "unnamed"),
                "currency": body.get("currency", "USD"),
            }
            self.accounts_db[acc_id] = account
            payload = json.dumps(account).encode("utf-8")
            self.send_response(HTTPStatus.CREATED)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Host-Token-Echo", self.headers.get("Authorization", ""))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()


def test_real_small_closed_loop_end_to_end(tmp_path: Path, monkeypatch) -> None:
    # 0. Start a real in-process loopback business server
    MiniBusinessServiceHandler.accounts_db.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MiniBusinessServiceHandler)
    server_port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        # Set up synthetic environment token
        monkeypatch.setenv("TESTPILOT_CLOSED_LOOP_TOKEN", "super-secret-token-12345")

        # Prepare OpenAPI specification
        spec_content = {
            "openapi": "3.0.3",
            "info": {"title": "Mini Accounts API", "version": "1.0.0"},
            "paths": {
                "/api/v1/accounts": {
                    "post": {
                        "operationId": "create_account",
                        "summary": "Create an account",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "currency": {"type": "string"},
                                        },
                                        "required": ["name", "currency"],
                                    }
                                }
                            },
                        },
                        "responses": {"201": {"description": "Account created"}},
                    }
                },
                "/api/v1/accounts/{account_id}": {
                    "get": {
                        "operationId": "get_account",
                        "summary": "Get account details",
                        "parameters": [
                            {
                                "name": "account_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "Account details"}},
                    }
                },
            },
        }
        spec_file = tmp_path / "openapi.json"
        spec_file.write_text(json.dumps(spec_content), encoding="utf-8")

        # Step 1: Initialize Task from empty test set
        store = TaskStore(tmp_path / "tasks")
        task_id = "closed-loop-01"
        task = Task(
            task_id=task_id,
            target_project="MiniAccounts",
            target_commit="b" * 40,
            scope=("*",),
            document_paths=("openapi.json",),
            environment="local_sandbox",
        )
        store.create(task)
        assert task.status == "created"
        assert len(store.cases(task_id)) == 0

        # Step 2: Discover Interfaces
        discover(store, task_id, materials_root=tmp_path)
        task = store.load(task_id)
        assert task.status == "discovered"
        inv = json.loads((store._folder(task_id) / "inventory.json").read_text("utf-8"))
        assert len(inv["operations"]) == 2

        op_create = next(
            op["operation_id"]
            for op in inv["operations"]
            if op["method"] == "POST" and op["path"] == "/api/v1/accounts"
        )
        op_get = next(
            op["operation_id"]
            for op in inv["operations"]
            if op["method"] == "GET" and "/api/v1/accounts" in op["path"]
        )

        # Step 3: Design Test Cases with Rationales and Assertions
        # (Chained scenario: prepare/create -> test/get
        # with reference {{step_create.body.account_id}})
        from api_test_agent.tasks.design import SCENARIOS, load_inventory

        loaded_inv = load_inventory(store, task_id)
        source = loaded_inv.materials[0]

        designed_cases_payload = {
            "cases": [
                {
                    "case_id": "create-and-get-account",
                    "revision": 1,
                    "title": "Create a new account and verify details",
                    "origin": "ai",
                    "target_commit": "b" * 40,
                    "operation_ids": [op_create, op_get],
                    "purpose": "Verify account created can be retrieved",
                    "scenario": "normal",
                    "rules": [
                        {
                            "rule_id": "acc-persistence",
                            "statement": "Created accounts must be queryable via GET",
                            "basis": "requirement",
                            "sources": [source.model_dump(mode="json")],
                        }
                    ],
                    "steps": [
                        {
                            "step_id": "step_create",
                            "phase": "prepare",
                            "operation_id": op_create,
                            "account": "synthetic_user",
                            "side_effect": True,
                            "parameters": [
                                {
                                    "name": "$",
                                    "location": "body",
                                    "mode": "literal",
                                    "value": {"name": "Checking Account", "currency": "USD"},
                                    "role": "target",
                                    "reason": "Valid account creation JSON payload",
                                    "source_ids": [source.source_id],
                                }
                            ],
                        },
                        {
                            "step_id": "step_get",
                            "phase": "test",
                            "operation_id": op_get,
                            "account": "synthetic_user",
                            "side_effect": False,
                            "parameters": [
                                {
                                    "name": "account_id",
                                    "location": "path",
                                    "mode": "reference",
                                    "value": "{{step_create.body.account_id}}",
                                    "role": "target",
                                    "reason": "Reference the newly created account id",
                                    "source_ids": [source.source_id],
                                }
                            ],
                        },
                    ],
                    "assertions": [
                        {
                            "expression": "{{step_get.status}} == 200",
                            "expected": True,
                            "rule_ids": ["acc-persistence"],
                        },
                        {
                            "expression": "{{step_get.body.name}} == 'Checking Account'",
                            "expected": True,
                            "rule_ids": ["acc-persistence"],
                        },
                    ],
                    "change_summary": "Initial design verified against OpenAPI spec",
                }
            ],
            "assessments": [
                {
                    "operation_id": op,
                    "scenario": s,
                    "status": "designed" if s == "normal" else "pending",
                    "reason": "Covered by normal test" if s == "normal" else "Needs scenario test",
                }
                for op in [op_create, op_get]
                for s in SCENARIOS
            ],
        }

        design(store, task_id, supplied_response=json.dumps(designed_cases_payload))
        task = store.load(task_id)
        assert task.status == "designed"
        cases = store.cases(task_id)
        assert len(cases) == 1
        case = cases[0]
        assert case.case_id == "create-and-get-account"

        # Step 4: Export Review Materials
        exec_config = ExecutionConfig(
            environment="local_sandbox",
            target_commit="b" * 40,
            base_url=f"http://127.0.0.1:{server_port}",
            allowed_operations=(op_create, op_get),
            accounts={"synthetic_user": "TESTPILOT_CLOSED_LOOP_TOKEN"},
            max_requests=20,
        )
        review_doc = export_review(store, task_id, exec_config)
        assert review_doc.is_file()
        task = store.load(task_id)
        assert task.status == "awaiting_review"

        manifest_file = review_doc.parent / "manifest.json"
        manifest = json.loads(manifest_file.read_text("utf-8"))

        # Step 5: Human Review Decision and Approval Freeze
        submission = ReviewSubmission(
            review_id=review_doc.parent.name,
            review_hash=fingerprint(manifest),
            actor="test-lead-engineer",
            decisions=[
                CaseDecision(
                    case_id=case.case_id,
                    revision_hash=case.fingerprint(),
                    decision="approved",
                    reason="Human verified: rationales and assertions are mathematically sound",
                )
            ],
        )

        approval_file = approve(store, task_id, submission, exec_config)
        assert (approval_file / "approval.json").is_file()
        task = store.load(task_id)
        assert task.status == "ready"

        # Step 6: Controlled Execution against Real Loopback Service
        accounting = execute_task(store, task_id, config=exec_config)
        assert accounting.passed == 1
        assert accounting.failed == 0
        assert accounting.error == 0
        assert accounting.blocked == 0
        assert accounting.not_run == 0
        assert accounting.conserved is True

        task = store.load(task_id)
        assert task.status == "completed"

        # Step 7: Report Generation and Strict Conservation Verification
        report_model, report_md_path, report_json_path = generate_report(store, task_id)
        assert report_md_path.is_file()
        assert report_json_path.is_file()

        assert report_model.accounting.conserved is True
        assert report_model.accounting.missing_evidence_count == 0
        assert report_model.accounting.planned == 1
        assert report_model.accounting.passed == 1
        assert report_model.accounting.failed == 0

        # Verification of gaps: all 2 operations were called, coverage rate 100%
        assert report_model.operation_coverage.total_discovered == 2
        assert report_model.operation_coverage.called == 2
        assert report_model.operation_coverage.coverage_rate == 1.0

        # Step 8: Strict Evidence Inspection and Token Redaction Check
        records = store.execution_records(task_id)
        assert len(records) == 1
        record = records[0]
        assert record.outcome == "passed"
        assert len(record.evidence_ids) == 2

        # Step-by-step evidence check
        for ev_id in record.evidence_ids:
            ev = store.load_evidence(task_id, ev_id)
            assert ev is not None
            assert ev.response_status in {200, 201}
            # Credential redaction check: the secret token must NOT be leaked in saved evidence
            ev_json = ev.model_dump_json()
            assert "super-secret-token-12345" not in ev_json
            assert "[REDACTED]" in ev_json

        # Check server DB: exactly 1 account created
        assert len(MiniBusinessServiceHandler.accounts_db) == 1
        acc = list(MiniBusinessServiceHandler.accounts_db.values())[0]
        assert acc["name"] == "Checking Account"
        assert acc["currency"] == "USD"

    finally:
        server.shutdown()
        server.server_close()
