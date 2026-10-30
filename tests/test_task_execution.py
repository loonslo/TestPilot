import json
from copy import deepcopy

import httpx
import pytest
from test_task_design import prepared as prepared

from api_test_agent.tasks.approval import ExecutionConfig, ReviewSubmission, approve
from api_test_agent.tasks.design import design, export_review
from api_test_agent.tasks.execution import execute_task


@pytest.fixture
def approved_setup(prepared):
    store, inventory, output = prepared
    # Add a second case to test multi-case scenarios
    case2 = deepcopy(output["cases"][0])
    case2["case_id"] = "list-items-2"
    case2["scenario"] = "boundary"
    output["cases"].append(case2)
    for a in output["assessments"]:
        if a["scenario"] == "boundary":
            a["status"] = "designed"
            a["reason"] = "Boundary check"

    design(store, "demo", supplied_response=json.dumps(output))
    config = ExecutionConfig(
        environment="synthetic",
        target_commit="a" * 40,
        base_url="http://127.0.0.1:18081",
        allowed_operations=[inventory.operations[0].operation_id],
        accounts={"synthetic": "SYNTHETIC_API_TOKEN"},
    )
    review = export_review(store, "demo", config)
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    cases = store.cases("demo")

    from api_test_agent.tasks.approval import fingerprint

    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human-reviewer",
        decisions=[
            dict(
                case_id=c.case_id,
                revision_hash=c.fingerprint(),
                decision="approved",
                reason="Reviewed synthetic baseline",
            )
            for c in cases
        ],
    )
    folder = approve(store, "demo", submission, config)
    return store, config, folder.name, inventory


def test_execution_refused_if_unapproved_or_tampered(approved_setup, monkeypatch):
    store, config, approval_id, _ = approved_setup

    def refuse_http(*args, **kwargs):
        pytest.fail("HTTP request must not be sent when execution is refused")

    client = httpx.Client(transport=httpx.MockTransport(refuse_http))

    # 1. Modify case revision after approval
    path = store._folder("demo") / "cases/list-items/00000001.json"
    data = json.loads(path.read_text("utf-8"))
    data["assertions"][0]["expected"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="case set|revision"):
        execute_task(store, "demo", approval_id=approval_id, config=config, http_client=client)

    assert store.load("demo").status == "ready"


def test_successful_execution_records_evidence_and_completes(approved_setup, monkeypatch):
    store, config, approval_id, _ = approved_setup
    monkeypatch.setenv("SYNTHETIC_API_TOKEN", "secret-test-token-12345")

    requests_sent = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        requests_sent.append(request)
        return httpx.Response(200, json={"items": [{"id": 1, "name": "item1"}]})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    accounting = execute_task(
        store, "demo", approval_id=approval_id, config=config, http_client=client
    )

    assert accounting.planned == 2
    assert accounting.passed == 2
    assert accounting.failed == 0
    assert accounting.error == 0
    assert accounting.blocked == 0
    assert accounting.not_run == 0
    assert accounting.conserved is True
    assert accounting.evidence_backed_count == 2
    assert len(requests_sent) == 2
    assert store.load("demo").status == "completed"

    # Verify evidence records exist and secret tokens are redacted
    active_exec = json.loads((store._folder("demo") / "active-execution.json").read_text("utf-8"))[
        "execution_id"
    ]
    evidence_files = list(
        (store._folder("demo") / f"executions/{active_exec}/evidence").glob("*.json")
    )
    assert len(evidence_files) == 2
    for ef in evidence_files:
        ev_data = json.loads(ef.read_text("utf-8"))
        assert ev_data["response_status"] == 200
        # Check token never leaks
        assert "secret-test-token-12345" not in json.dumps(ev_data)
        if "authorization" in [k.lower() for k in ev_data.get("response_headers", {})]:
            assert ev_data["response_headers"]["authorization"] == "[REDACTED]"


def test_assertion_failure_marks_case_failed_and_creates_issue(approved_setup):
    store, config, approval_id, _ = approved_setup

    def mock_handler(request: httpx.Request) -> httpx.Response:
        # Return 400 instead of 200
        return httpx.Response(400, json={"error": "Bad request"})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    accounting = execute_task(
        store, "demo", approval_id=approval_id, config=config, http_client=client
    )

    assert accounting.planned == 2
    assert accounting.passed == 0
    assert accounting.failed == 2
    assert accounting.conserved is True
    assert store.load("demo").status == "completed"

    active_exec = json.loads((store._folder("demo") / "active-execution.json").read_text("utf-8"))[
        "execution_id"
    ]
    issues = store.issues("demo", active_exec)
    assert len(issues) == 2
    assert issues[0].category in {"suspected_defect", "unconfirmed_hypothesis"}
    assert "Assertion failed" in issues[0].detail


def test_interruption_with_max_cases_marks_not_run_and_status_incomplete(approved_setup):
    store, config, approval_id, _ = approved_setup

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    accounting = execute_task(
        store,
        "demo",
        approval_id=approval_id,
        config=config,
        max_cases=1,
        http_client=client,
    )

    assert accounting.planned == 2
    assert accounting.passed == 1
    assert accounting.not_run == 1
    assert accounting.conserved is True
    assert store.load("demo").status == "incomplete"


def test_missing_host_credential_blocks_case_without_sending_request(approved_setup, monkeypatch):
    store, config, approval_id, _ = approved_setup
    monkeypatch.delenv("SYNTHETIC_API_TOKEN", raising=False)

    # Change case to require account="synthetic"
    path = store._folder("demo") / "cases/list-items/00000001.json"
    data = json.loads(path.read_text("utf-8"))
    data["steps"][0]["account"] = "synthetic"
    path.write_text(json.dumps(data), encoding="utf-8")

    # Update approval to match the new case
    from api_test_agent.tasks.approval import fingerprint

    cases = store.cases("demo")
    review = export_review(store, "demo", config)
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human-reviewer",
        decisions=[
            dict(
                case_id=c.case_id,
                revision_hash=c.fingerprint(),
                decision="approved",
                reason="Reviewed synthetic baseline",
            )
            for c in cases
        ],
    )
    folder = approve(store, "demo", submission, config)

    requests_sent = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        requests_sent.append(request)
        return httpx.Response(200, json={"items": []})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    accounting = execute_task(
        store, "demo", approval_id=folder.name, config=config, http_client=client
    )

    assert accounting.blocked >= 1
    assert accounting.conserved is True
    # At least the blocked case sent no request
    assert len(requests_sent) < 2


def test_max_requests_enforced_strictly(approved_setup):
    store, base_config, _, _ = approved_setup

    # Config with max_requests=1
    config = ExecutionConfig(
        environment=base_config.environment,
        target_commit=base_config.target_commit,
        base_url=base_config.base_url,
        allowed_operations=base_config.allowed_operations,
        accounts=base_config.accounts,
        max_requests=1,
    )
    review = export_review(store, "demo", config)
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    cases = store.cases("demo")
    from api_test_agent.tasks.approval import fingerprint

    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human-reviewer",
        decisions=[
            dict(
                case_id=c.case_id,
                revision_hash=c.fingerprint(),
                decision="approved",
                reason="Reviewed synthetic baseline with max_requests=1",
            )
            for c in cases
        ],
    )
    folder = approve(store, "demo", submission, config)

    requests_sent = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        requests_sent.append(request)
        return httpx.Response(200, json={"items": [{"id": 1}]})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    accounting = execute_task(
        store, "demo", approval_id=folder.name, config=config, http_client=client
    )

    assert len(requests_sent) == 1
    assert accounting.planned == 2
    assert accounting.passed == 1
    assert accounting.not_run == 1
    assert accounting.conserved is True
    assert store.load("demo").status == "incomplete"


def test_resume_incomplete_task_does_not_repeat_side_effects(approved_setup):
    store, config, approval_id, _ = approved_setup

    requests_sent = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        requests_sent.append(request.url.path)
        return httpx.Response(200, json={"items": [{"id": 1}]})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    # First run with max_cases=1
    acc1 = execute_task(
        store, "demo", approval_id=approval_id, config=config, max_cases=1, http_client=client
    )
    assert acc1.passed == 1
    assert acc1.not_run == 1
    assert len(requests_sent) == 1
    assert store.load("demo").status == "incomplete"

    # Second run without recreating: resumes remaining cases
    acc2 = execute_task(store, "demo", approval_id=approval_id, config=config, http_client=client)
    assert acc2.passed == 2
    assert acc2.not_run == 0
    assert acc2.conserved is True
    # Only 1 additional request was sent for the second case; case 1 was NOT re-run!
    assert len(requests_sent) == 2
    assert store.load("demo").status == "completed"


def test_missing_parameter_reference_does_not_crash_accounting(approved_setup):
    store, config, approval_id, _ = approved_setup

    # Tamper case to add step2 that references an earlier step's missing field at runtime
    path = store._folder("demo") / "cases/list-items/00000001.json"
    data = json.loads(path.read_text("utf-8"))
    # Add step2 that depends on request, referencing a nonexistent property in response
    step2 = deepcopy(data["steps"][0])
    step2["step_id"] = "step2"
    step2["depends_on"] = ["request"]
    step2["parameters"] = [
        {
            "name": "ref_param",
            "location": "query",
            "mode": "reference",
            "value": "{{request.missing_field_in_response}}",
            "role": "target",
            "reason": "Referencing field missing from runtime response",
            "source_ids": [data["rules"][0]["sources"][0]["source_id"]],
        }
    ]
    data["steps"].append(step2)
    path.write_text(json.dumps(data), encoding="utf-8")

    from api_test_agent.tasks.approval import fingerprint

    cases = store.cases("demo")
    review = export_review(store, "demo", config)
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    submission = ReviewSubmission(
        review_id=review.parent.name,
        review_hash=fingerprint(manifest),
        actor="synthetic-human-reviewer",
        decisions=[
            dict(
                case_id=c.case_id,
                revision_hash=c.fingerprint(),
                decision="approved",
                reason="Approved for reference test",
            )
            for c in cases
        ],
    )
    folder = approve(store, "demo", submission, config)

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    # Execute should NOT crash; parameter resolution error should be caught and recorded
    accounting = execute_task(
        store, "demo", approval_id=folder.name, config=config, http_client=client
    )

    assert accounting.error >= 1
    assert accounting.conserved is True
    assert store.load("demo").status == "completed"


def test_complete_token_redaction_in_response_and_issues(approved_setup, monkeypatch):
    store, config, approval_id, _ = approved_setup
    secret_token = "ultra-secret-token-xyz-987"
    monkeypatch.setenv("SYNTHETIC_API_TOKEN", secret_token)

    # Server echoes the secret token in response body and custom header
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": f"echo {secret_token}", "nested": {"token": secret_token}},
            headers={"X-Server-Echo": f"token-{secret_token}"},
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    accounting = execute_task(
        store, "demo", approval_id=approval_id, config=config, http_client=client
    )

    assert accounting.failed == 2
    active_exec = json.loads((store._folder("demo") / "active-execution.json").read_text("utf-8"))[
        "execution_id"
    ]
    exec_dir = store._folder("demo") / "executions" / active_exec

    # Scan all files in execution directory: none may contain the secret token!
    for f in exec_dir.glob("**/*"):
        if f.is_file():
            content = f.read_text(encoding="utf-8")
            assert secret_token not in content, f"Secret token leaked into {f.name}"


def test_keyboard_interrupt_completes_accounting_and_transitions_incomplete(approved_setup):
    store, config, approval_id, _ = approved_setup

    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise KeyboardInterrupt("Simulated human cancel")
        return httpx.Response(200, json={"items": [{"id": 1}]})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    with pytest.raises(KeyboardInterrupt):
        execute_task(store, "demo", approval_id=approval_id, config=config, http_client=client)

    # Task status must be incomplete, not stuck in running!
    assert store.load("demo").status == "incomplete"

    active_exec = json.loads((store._folder("demo") / "active-execution.json").read_text("utf-8"))[
        "execution_id"
    ]
    exec_dir = store._folder("demo") / "executions" / active_exec
    summary = json.loads((exec_dir / "execution.json").read_text("utf-8"))
    accounting = summary["accounting"]

    assert accounting["planned"] == 2
    assert accounting["passed"] == 1
    assert accounting["not_run"] == 0
    assert accounting["error"] == 1
    assert accounting["conserved"] is True


def test_executing_task_in_running_state_is_refused(approved_setup):
    store, config, approval_id, _ = approved_setup

    store.transition("demo", "running", "Task left in running state")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with pytest.raises(ValueError, match="already in 'running' state"):
        execute_task(store, "demo", approval_id=approval_id, config=config, http_client=client)


def test_interrupt_first_request_is_not_retried_on_resume(approved_setup):
    store, config, approval_id, _ = approved_setup
    sent = []

    def handler(request):
        sent.append(request.url.path)
        if len(sent) == 1:
            raise KeyboardInterrupt("cancel after possible side effect")
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(KeyboardInterrupt):
            execute_task(store, "demo", http_client=client)
        records = store.execution_records("demo")
        assert sorted(r.outcome for r in records) == ["error", "not_run"]
        acc = execute_task(store, "demo", http_client=client)
    assert len(sent) == 2
    assert acc.error == 1
    assert acc.passed == 1
    assert acc.conserved


def test_budget_is_preserved_on_resume(approved_setup):
    store, config, _, _ = approved_setup
    config = config.model_copy(update={"max_requests": 1})
    review = export_review(store, "demo", config)
    from api_test_agent.tasks.approval import fingerprint

    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    approve(
        store,
        "demo",
        ReviewSubmission(
            review_id=review.parent.name,
            review_hash=fingerprint(manifest),
            actor="synthetic-reviewer",
            decisions=[
                dict(
                    case_id=c.case_id,
                    revision_hash=c.fingerprint(),
                    decision="approved",
                    reason="Budget test",
                )
                for c in store.cases("demo")
            ],
        ),
        config,
    )
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execute_task(store, "demo", http_client=client)
        acc = execute_task(store, "demo", http_client=client)
    assert len(sent) == 1
    assert acc.passed == 1
    assert acc.not_run == 1


def test_short_secret_and_dictionary_keys_are_redacted():
    from api_test_agent.tasks.execution import redact_sensitive

    assert redact_sensitive({"echo-abc": {"value": "abc"}}, {"abc"}) == {
        "echo-[REDACTED]": {"value": "[REDACTED]"}
    }


def test_echoed_token_references_and_assertions_use_raw_memory(approved_setup, monkeypatch):
    store, config, _, _ = approved_setup
    secret = "xyz"
    monkeypatch.setenv("SYNTHETIC_API_TOKEN", secret)
    path = store._folder("demo") / "cases/list-items/00000001.json"
    data = json.loads(path.read_text("utf-8"))
    followup = deepcopy(data["steps"][0])
    followup["step_id"] = "followup"
    followup["depends_on"] = ["request"]
    followup["parameters"] = [
        dict(
            name="echo",
            location="query",
            mode="reference",
            value="{{request.body.echo}}",
            role="target",
            reason="Use the echoed synthetic value",
            source_ids=[data["rules"][0]["sources"][0]["source_id"]],
        )
    ]
    data["steps"].append(followup)
    data["assertions"][0]["expression"] = "{{followup.body.echo}}"
    data["assertions"][0]["expected"] = "expected-safe-value"
    path.write_text(json.dumps(data), encoding="utf-8")
    review = export_review(store, "demo", config)
    from api_test_agent.tasks.approval import fingerprint

    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    approve(
        store,
        "demo",
        ReviewSubmission(
            review_id=review.parent.name,
            review_hash=fingerprint(manifest),
            actor="reviewer",
            decisions=[
                dict(
                    case_id=c.case_id,
                    revision_hash=c.fingerprint(),
                    decision="approved",
                    reason="Synthetic echo test",
                )
                for c in store.cases("demo")
            ],
        ),
        config,
    )
    echoed = []

    def handler(request):
        if "echo" in request.url.params:
            echoed.append(request.url.params["echo"])
        return httpx.Response(200, json={"echo": secret, secret: "echoed key", "items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        accounting = execute_task(store, "demo", http_client=client)
    assert echoed == [secret]
    assert accounting.failed == 1
    from api_test_agent.tasks.report import generate_report

    generate_report(store, "demo")
    for path in store._folder("demo").rglob("*"):
        if path.is_file():
            assert secret not in path.read_text("utf-8"), path


@pytest.mark.parametrize("main_status", [200, 400])
def test_cleanup_chain_can_reference_prior_cleanup_response(prepared, main_status):
    store, inventory, output = prepared
    first = output["cases"][0]
    source_id = first["rules"][0]["sources"][0]["source_id"]
    for step_id, mode, name, value, depends_on in [
        ("clean1", "literal", "cleanup", "first", ["request"]),
        ("clean2", "reference", "cleanup_ref", "{{clean1.body.id}}", ["clean1"]),
    ]:
        first["steps"].append(
            dict(
                step_id=step_id,
                phase="cleanup",
                operation_id=inventory.operations[0].operation_id,
                account="anonymous",
                side_effect=False,
                depends_on=depends_on,
                parameters=[
                    dict(
                        name=name,
                        location="query",
                        mode=mode,
                        value=value,
                        role="background",
                        reason="Clean the synthetic fixture",
                        source_ids=[source_id],
                    )
                ],
            )
        )
    second = deepcopy(first)
    second["case_id"] = "list-items-2"
    output["cases"].append(second)
    design(store, "demo", supplied_response=json.dumps(output))
    config = ExecutionConfig(
        environment="synthetic",
        target_commit="a" * 40,
        base_url="http://127.0.0.1:18081",
        allowed_operations=[inventory.operations[0].operation_id],
    )
    review = export_review(store, "demo", config)
    from api_test_agent.tasks.approval import fingerprint

    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    approve(
        store,
        "demo",
        ReviewSubmission(
            review_id=review.parent.name,
            review_hash=fingerprint(manifest),
            actor="reviewer",
            decisions=[
                dict(
                    case_id=c.case_id,
                    revision_hash=c.fingerprint(),
                    decision="approved",
                    reason="Review synthetic cleanup chain",
                )
                for c in store.cases("demo")
            ],
        ),
        config,
    )
    sent = []
    references = []

    def handler(request):
        sent.append(request)
        if "cleanup" in request.url.params:
            return httpx.Response(200, json={"id": len(sent)})
        if "cleanup_ref" in request.url.params:
            references.append(request.url.params["cleanup_ref"])
            return httpx.Response(200, json={"cleaned": True})
        return httpx.Response(main_status, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        accounting = execute_task(store, "demo", http_client=client)
    assert len(sent) == 6
    assert references == ["2", "5"]
    assert accounting.passed == (2 if main_status == 200 else 0)
    assert accounting.failed == (0 if main_status == 200 else 2)
    assert accounting.conserved
    assert not any(issue.category == "cleanup_failure" for issue in store.issues("demo"))
    for record in store.execution_records("demo"):
        assert len(record.evidence_ids) == 3
        evidence = [store.load_evidence("demo", eid) for eid in record.evidence_ids]
        assert all(
            ev.request_attempted and ev.response_received and ev.error is None for ev in evidence
        )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
@pytest.mark.parametrize("body_mode", ["literal", "reference", "omitted", "absent"])
def test_explicit_json_null_is_distinct_from_absent_body(method, body_mode):
    from api_test_agent.tasks.execution import execute_step
    from api_test_agent.tasks.models import Parameter, Step

    parameters = (
        ()
        if body_mode == "absent"
        else (
            Parameter(
                name="$",
                location="body",
                mode=body_mode,
                value="{{prior.body.nullable}}" if body_mode == "reference" else None,
                role="target",
                reason="Test null versus omission",
                source_ids=["synthetic"],
            ),
        )
    )
    step = Step(
        step_id="request",
        phase="test",
        operation_id="synthetic-op",
        account="anonymous",
        side_effect=True,
        parameters=parameters,
    )
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, evidence = execute_step(
            client,
            "http://127.0.0.1:18081",
            {"method": method, "path": "/items"},
            step,
            {"prior": {"body": {"nullable": None}}},
            {},
            10,
            1000,
        )
    assert len(sent) == 1
    assert evidence.error is None
    assert evidence.request_parameters["body_provided"] == (body_mode in {"literal", "reference"})
    if body_mode in {"literal", "reference"}:
        assert sent[0].content == b"null"
        assert sent[0].headers["content-type"] == "application/json"
    else:
        assert sent[0].content == b""
        assert "content-type" not in sent[0].headers


def test_named_null_body_field_is_preserved():
    from api_test_agent.tasks.execution import execute_step
    from api_test_agent.tasks.models import Parameter, Step

    step = Step(
        step_id="request",
        phase="test",
        operation_id="synthetic-op",
        account="anonymous",
        side_effect=True,
        parameters=(
            Parameter(
                name="nullable",
                location="body",
                mode="literal",
                value=None,
                role="target",
                reason="Test a null field",
                source_ids=["synthetic"],
            ),
        ),
    )
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execute_step(
            client,
            "http://127.0.0.1:18081",
            {"method": "POST", "path": "/items"},
            step,
            {},
            {},
            10,
            1000,
        )
    assert json.loads(sent[0].content) == {"nullable": None}
