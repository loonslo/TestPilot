import json
from copy import deepcopy

import pytest

from api_test_agent.cli import main
from api_test_agent.tasks.design import (
    SCENARIOS,
    design,
    design_request,
    export_review,
    load_inventory,
)
from api_test_agent.tasks.discovery import discover
from api_test_agent.tasks.models import CaseRevision, Task
from api_test_agent.tasks.store import TaskStore


@pytest.fixture
def prepared(tmp_path):
    (tmp_path / "api.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {
                    "/items": {
                        "get": {
                            "operationId": "items",
                            "parameters": [
                                {
                                    "name": "limit",
                                    "in": "query",
                                    "schema": {"type": "integer", "minimum": 1},
                                }
                            ],
                            "responses": {"200": {"description": "Return items"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = TaskStore(tmp_path / "tasks")
    store.create(
        Task(
            task_id="demo",
            target_project="synthetic",
            target_commit="a" * 40,
            scope=["*"],
            document_paths=["api.json"],
        )
    )
    discover(store, "demo", tmp_path)
    inv = load_inventory(store, "demo")
    op = inv.operations[0]
    source = inv.materials[0]
    case = dict(
        case_id="list-items",
        revision=1,
        title="List synthetic items",
        origin="ai",
        target_commit="a" * 40,
        operation_ids=[op.operation_id],
        purpose="Valid baseline",
        scenario="normal",
        rules=[
            dict(
                rule_id="list-status",
                statement="Return 200",
                basis="requirement",
                sources=[source.model_dump(mode="json")],
            )
        ],
        steps=[
            dict(
                step_id="request",
                phase="test",
                operation_id=op.operation_id,
                account="anonymous",
                side_effect=False,
                parameters=[
                    dict(
                        name="limit",
                        location="query",
                        mode="literal",
                        value=1,
                        role="background",
                        reason="Use documented lower bound as a valid baseline",
                        source_ids=[source.source_id],
                    )
                ],
            )
        ],
        assertions=[
            dict(expression="{{request.status}} == 200", expected=True, rule_ids=["list-status"])
        ],
        change_summary="Initial design",
    )
    output = dict(
        cases=[case],
        assessments=[
            dict(
                operation_id=op.operation_id,
                scenario=s,
                status="designed" if s == "normal" else "pending",
                reason="Design exists" if s == "normal" else "Needs additional rule confirmation",
            )
            for s in SCENARIOS
        ],
    )
    return store, inv, output


def test_from_empty_assets_to_validated_drafts_and_review(prepared, monkeypatch):
    import httpx

    def refuse_request(*args, **kwargs):
        pytest.fail("Design or review must not send HTTP")

    monkeypatch.setattr(httpx.Client, "request", refuse_request)
    store, inv, output = prepared
    assert store.cases("demo") == []
    received = []

    def fake_generator(request):
        received.append(json.loads(request))
        return json.dumps(output)

    folder = design(store, "demo", fake_generator)
    assert (folder / "validated.json").is_file()
    assert len(received) == 1
    assert received[0]["materials"]["materials"][0]["content"] == inv.snapshots["api.json"]
    assert store.load("demo").status == "designed"
    assert store.review_states("demo") == {"list-items": "draft"}
    review = export_review(store, "demo")
    text = review.read_text("utf-8")
    assert "Use documented lower bound" in text
    assert "场景设计矩阵" in text and "Needs additional rule confirmation" in text
    assert store.load("demo").status == "awaiting_review"
    assert store.review_states("demo") == {"list-items": "pending_review"}
    manifest = json.loads((review.parent / "manifest.json").read_text("utf-8"))
    assert manifest["executable"] is False
    assert manifest["cases"] == {"list-items": store.cases("demo")[0].fingerprint()}
    assert not list(store._folder("demo").glob("**/execution*.json"))


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_source",
        "reason",
        "matrix",
        "approval",
        "expression",
        "credentials",
        "wrong_version",
        "empty_literal",
    ],
)
def test_invalid_model_output_saved_but_no_cases_imported(prepared, mutation):
    store, inv, original = prepared
    output = deepcopy(original)
    case = output["cases"][0]
    if mutation == "unknown_source":
        case["rules"][0]["sources"][0]["content_hash"] = "f" * 64
    elif mutation == "reason":
        case["steps"][0]["parameters"][0]["reason"] = ""
    elif mutation == "matrix":
        output["assessments"].pop()
    elif mutation == "approval":
        case["approved"] = True
    elif mutation == "expression":
        case["assertions"][0]["expression"] = "__import__('os')"
    elif mutation == "credentials":
        case["steps"][0]["parameters"][0].update(name="Authorization", location="header")
    elif mutation == "wrong_version":
        case["target_commit"] = "b" * 40
    else:
        case["steps"][0]["parameters"][0].pop("value")
    with pytest.raises(ValueError):
        design(store, "demo", supplied_response=json.dumps(output))
    assert store.cases("demo") == []
    assert store.load("demo").status == "discovered"
    failures = list(store._folder("demo").glob("designs/*/failed.json"))
    assert len(failures) == 1
    assert (failures[0].parent / "response.json").is_file()


def test_generator_failure_and_request_only_keep_task_truthful(prepared):
    store, _, _ = prepared
    folder = design(store, "demo")
    assert (folder / "request.json").is_file()
    assert not (folder / "response.json").exists()

    def failed_generator(request):
        raise RuntimeError("Synthetic vendor unavailable")

    with pytest.raises(RuntimeError):
        design(store, "demo", failed_generator)
    assert list(store._folder("demo").glob("designs/*/failed.json"))
    assert store.load("demo").status == "discovered"
    assert store.cases("demo") == []


def test_final_request_budget_includes_schema_and_inventory(prepared):
    _, inv, _ = prepared
    prompt = design_request(inv)
    assert len(prompt) <= 60000
    with pytest.raises(ValueError, match="budget"):
        design_request(inv, 100)


def test_cli_design_response_then_review(prepared, tmp_path):
    store, _, output = prepared
    response = tmp_path / "synthetic-response.json"
    response.write_text(json.dumps(output), encoding="utf-8")
    prefix = ["task", "--root", str(store.root)]
    assert main([*prefix, "design", "demo", "--response", str(response)]) == 0
    assert main([*prefix, "review", "demo"]) == 0
    assert store.review_states("demo") == {"list-items": "pending_review"}


def test_new_revision_returns_to_draft_without_altering_old_review(prepared):
    store, _, output = prepared
    design(store, "demo", supplied_response=json.dumps(output))
    first_review = export_review(store, "demo")
    saved = first_review.read_bytes()
    original = store.cases("demo")[0]
    data = original.model_dump(mode="json") | {
        "revision": 2,
        "supersedes": original.fingerprint(),
        "change_summary": "Clarify baseline reason",
    }
    data["steps"][0]["parameters"][0]["reason"] = "Revised explanation"
    store.add_case("demo", CaseRevision(**data))
    assert store.review_states("demo") == {"list-items": "draft"}
    export_review(store, "demo")
    assert first_review.read_bytes() == saved
    assert len(store.review_history("demo")) == 2


def test_live_adapter_budget_and_model_metadata_with_fake_provider(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from api_test_agent.tasks import design_model

    monkeypatch.chdir(tmp_path)
    config = tmp_path / "model.toml"
    config.write_text(
        '[model]\nmodel_id="openrouter/free"\n[budget]\nmax_requests_per_day=1\n', encoding="utf-8"
    )
    monkeypatch.setattr(design_model, "read_env", lambda name: "synthetic-key-for-fake-client")
    calls = []

    class FakeClient:
        model_id = "openrouter/free"

        def __init__(self, settings, key):
            pass

        def chat(self, messages, tools, **kwargs):
            calls.append(messages)
            return SimpleNamespace(
                content='{"cases":[]}',
                tool_calls=(),
                served_by="fake-provider",
                usage=SimpleNamespace(total_tokens=20, cost_usd=0),
                latency_seconds=0.01,
                finish_reason="stop",
            )

    monkeypatch.setattr(design_model, "ModelClient", FakeClient)
    generator = design_model.live_generator(config, sleep=lambda seconds: None)
    reply = generator("synthetic-request")
    assert reply.metadata["model"] == "fake-provider"
    assert reply.metadata["budget"]["model_calls"] == 1
    with pytest.raises(ValueError, match="daily_requests"):
        generator("second request refused")
    assert len(calls) == 1
    ledger = (tmp_path / "runtime/model-usage.json").read_text("utf-8")
    assert "synthetic-key-for-fake-client" not in ledger


def test_live_adapter_refuses_paid_config_without_reading_key(tmp_path, monkeypatch):
    from api_test_agent.tasks import design_model

    config = tmp_path / "model.toml"
    config.write_text('[model]\nmodel_id="paid-model"\n', encoding="utf-8")
    monkeypatch.setattr(design_model, "read_env", lambda name: pytest.fail("must not read key"))
    with pytest.raises(ValueError, match="free router"):
        design_model.live_generator(config)


def test_small_batch_keeps_full_inventory_gaps(prepared):
    from api_test_agent.tasks.design import DesignOutput, scenario_matrix, validate_output

    store, inv, output = prepared
    original = inv.operations[0]
    second = original.model_copy(update={"operation_id": "second", "path": "/second"})
    expanded = inv.model_copy(update={"operations": (original, second)})
    request = design_request(expanded, operation_ids={original.operation_id})
    assert len(json.loads(request)["operations"]) == 1
    validate_output(DesignOutput.model_validate(output), expanded, request)
    matrix = scenario_matrix(store, "demo", expanded, [CaseRevision(**output["cases"][0])])
    assert len(matrix) == 14
    assert all(row["status"] == "pending" for row in matrix if row["operation_id"] == "second")
    with pytest.raises(ValueError):
        design_request(expanded, operation_ids={"not-selected"})


def test_normal_invalid_and_boundary_designs_have_distinct_inputs_and_reasons(prepared):
    store, _, baseline = prepared
    output = deepcopy(baseline)
    normal = output["cases"][0]
    normal["case_id"] = "normal-limit"
    boundary = deepcopy(normal)
    boundary.update(case_id="boundary-limit", scenario="boundary", purpose="Check lower boundary")
    boundary["steps"][0]["parameters"][0].update(
        role="target", value=1, reason="Use the documented minimum itself as the valid boundary"
    )
    invalid = deepcopy(normal)
    invalid.update(
        case_id="invalid-limit", scenario="validation", purpose="Below minimum hypothesis"
    )
    invalid["steps"][0]["parameters"][0].update(
        role="target",
        value=0,
        reason="One below the documented minimum; rejection response still needs confirmation",
    )
    invalid["rules"][0].update(
        basis="hypothesis", statement="Hypothesis: reject below-minimum limit"
    )
    invalid["assertions"][0]["expression"] = "{{request.status}} >= 400"
    output["cases"] = [normal, boundary, invalid]
    for assessment in output["assessments"]:
        if assessment["scenario"] in {"normal", "boundary", "validation"}:
            assessment.update(
                status="designed", reason="Synthetic draft with distinct design target"
            )
    design(store, "demo", supplied_response=json.dumps(output))
    cases = store.cases("demo")
    assert len(cases) == 3
    assert {c.scenario for c in cases} == {"normal", "boundary", "validation"}
    assert next(c for c in cases if c.scenario == "validation").rules[0].basis == "hypothesis"
    path = export_review(store, "demo")
    text = path.read_text("utf-8")
    assert "参数选择理由" in text and "预期与断言" in text
    assert "rejection response still needs confirmation" in text


def test_budget_tracks_parsed_declarations_when_full_file_is_omitted(prepared):
    from api_test_agent.tasks.design import DesignOutput, validate_output
    from api_test_agent.tasks.discovery import Inventory, ref

    store, inv, output = prepared
    content = inv.snapshots["api.json"] + " " * 100000
    source = ref("api.json", content, inv.target_commit)
    data = inv.model_dump(mode="json")
    data["materials"] = [source.model_dump(mode="json")]
    data["snapshots"]["api.json"] = content
    data["operations"][0]["sources"] = [source.model_dump(mode="json")]
    expanded = Inventory.model_validate(data)
    maximum = len(design_request(inv)) + 1200
    request = design_request(expanded, maximum)
    payload = json.loads(request)
    assert len(request) <= maximum
    assert payload["materials"]["materials"] == []
    assert payload["material_delivery"][0]["mode"] == "parsed_declarations"
    assert payload["material_delivery"][0]["fragment_pointers"]
    output["cases"][0]["rules"][0]["sources"] = [source.model_dump(mode="json")]
    validate_output(DesignOutput.model_validate(output), expanded, request)
    payload["material_delivery"][0]["mode"] = "full_file"
    with pytest.raises(ValueError, match="delivery record"):
        validate_output(DesignOutput.model_validate(output), expanded, json.dumps(payload))


def test_interrupted_import_blocks_review_then_recovers_without_regeneration(prepared, monkeypatch):
    from api_test_agent.tasks.design import recover_design

    store, _, output = prepared
    second = deepcopy(output["cases"][0])
    second["case_id"] = "second-case"
    output["cases"].append(second)
    original = store.add_case
    calls = 0

    def interrupted(task_id, case):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Synthetic interrupted import")
        return original(task_id, case)

    monkeypatch.setattr(store, "add_case", interrupted)
    with pytest.raises(OSError):
        design(store, "demo", supplied_response=json.dumps(output))
    assert len(store.cases("demo")) == 1
    with pytest.raises(ValueError, match="interrupted design"):
        export_review(store, "demo")
    attempt = next(store._folder("demo").glob("designs/*/import-started.json")).parent
    first = store.cases("demo")[0].fingerprint()
    monkeypatch.setattr(store, "add_case", original)
    recover_design(store, "demo", attempt.name)
    assert len(store.cases("demo")) == 2
    assert store.cases("demo")[0].fingerprint() == first
    assert (attempt / "failed.json").is_file()
    assert (attempt / "import-completed.json").is_file()
    assert export_review(store, "demo").is_file()


def test_generation_interrupt_leaves_explicit_failure(prepared):
    store, _, _ = prepared

    def interrupted(request):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        design(store, "demo", interrupted)
    marker = next(store._folder("demo").glob("designs/*/failed.json"))
    assert json.loads(marker.read_text("utf-8"))["error_type"] == "KeyboardInterrupt"
    assert (marker.parent / "generation-started.json").is_file()
    assert store.cases("demo") == []


def test_provider_reported_cost_stops_before_drafts_are_imported(prepared, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from api_test_agent.tasks import design_model

    store, _, output = prepared
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "model.toml"
    config.write_text('[model]\nmodel_id="openrouter/free"\n', encoding="utf-8")
    monkeypatch.setattr(design_model, "read_env", lambda name: "synthetic-fake-client-key")

    class FakePaidReply:
        model_id = "openrouter/free"

        def __init__(self, settings, key):
            pass

        def chat(self, *args, **kwargs):
            return SimpleNamespace(
                content=json.dumps(output),
                tool_calls=(),
                served_by="fake-provider",
                usage=SimpleNamespace(total_tokens=100, cost_usd=0.01),
                latency_seconds=0.01,
                finish_reason="stop",
            )

    monkeypatch.setattr(design_model, "ModelClient", FakePaidReply)
    generator = design_model.live_generator(config, sleep=lambda seconds: None)
    with pytest.raises(ValueError, match="cost"):
        design(store, "demo", generator)
    assert store.cases("demo") == []
    marker = next(store._folder("demo").glob("designs/*/model-call.json"))
    assert json.loads(marker.read_text("utf-8"))["budget"]["cost_usd"] == 0.01
    assert "synthetic-fake-client-key" not in marker.read_text("utf-8")
