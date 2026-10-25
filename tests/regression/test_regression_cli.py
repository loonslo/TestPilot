from __future__ import annotations

import json

from conftest import make_suite

from api_test_agent.regression.cli import main


def _run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_plan_and_run_end_to_end(worlds, capsys):
    root, repo, _, _, _ = worlds
    make_suite(repo)
    config = str(root / "testagent.toml")

    code, out = _run(
        capsys,
        "plan",
        "--config",
        config,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--stub",
        "--run-id",
        "t1",
    )
    assert code == 0, out
    assert "已生成计划" in out
    plan_path = root / "artifacts" / "t1" / "plan.json"
    assert plan_path.is_file()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["state"] == "planned"

    # 计划里 TC-B 是刻意写坏的断言，执行后必须如实报失败，因此退出码为 1
    code, out = _run(
        capsys, "run", "t1", "--config", config, "--env-version", "stage-1", "--skip-confirm"
    )
    assert code == 1, out
    assert "失败 1" in out
    record = json.loads((root / "artifacts" / "t1" / "run.json").read_text(encoding="utf-8"))
    results = {r["case_id"]: r["result"] for r in record["results"]}
    assert results["TC-A"] == "passed"
    assert results["TC-B"] == "failed"
    assert (root / "artifacts" / "t1" / "report.md").is_file()

    code, out = _run(capsys, "runs", "--config", config)
    assert code == 0
    assert "t1" in out


def test_run_requires_environment_version(worlds, capsys):
    root, repo, _, _, _ = worlds
    make_suite(repo)
    config = str(root / "testagent.toml")
    _run(
        capsys,
        "plan",
        "--config",
        config,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--stub",
        "--run-id",
        "t2",
    )
    code, out = _run(capsys, "run", "t2", "--config", config, "--skip-confirm")
    assert code == 2
    assert "env-version" in out


def test_run_refuses_when_head_moved(worlds, capsys):
    from conftest import commit

    root, repo, _, _, _ = worlds
    make_suite(repo)
    config = str(root / "testagent.toml")
    _run(
        capsys,
        "plan",
        "--config",
        config,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--stub",
        "--run-id",
        "t3",
    )
    commit(repo, {"src/orders/api.py": "def create_order(q):\n    return {}\n"}, "new commit")
    code, out = _run(
        capsys, "run", "t3", "--config", config, "--env-version", "x", "--skip-confirm"
    )
    assert code == 1
    assert "代码已变化" in out


def test_run_refuses_tampered_plan(worlds, capsys):
    root, repo, _, _, _ = worlds
    make_suite(repo)
    config = str(root / "testagent.toml")
    _run(
        capsys,
        "plan",
        "--config",
        config,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--stub",
        "--run-id",
        "t4",
    )
    plan_path = root / "artifacts" / "t4" / "plan.json"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    manual = {
        "case_id": "TC-B",
        "name": "人工追加：库存不足报错",
        "module": "订单",
        "entry": ["tests/test_orders.py::test_stock_low"],
        "priority": "manual",
        "reason": "人工补充",
    }
    data["entries"].append(manual)
    plan_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    code, out = _run(
        capsys, "run", "t4", "--config", config, "--env-version", "x", "--skip-confirm"
    )
    assert code == 1
    assert "指纹" in out

    code, out = _run(capsys, "reconcile", "t4", "--config", config)
    assert code == 0, out
    code, out = _run(
        capsys, "run", "t4", "--config", config, "--env-version", "x", "--skip-confirm"
    )
    assert code == 1, out  # TC-B 断言失败，退出码非 0 才算如实
    assert "执行 3/3" in out or "执行 2/2" in out


def test_plan_requires_model_or_stub(worlds, capsys, monkeypatch):
    root, _, _, _, _ = worlds
    config_path = root / "testagent.toml"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.split("[model]")[0], encoding="utf-8")
    code, out = _run(
        capsys, "plan", "--config", str(config_path), "--base", "HEAD~1", "--head", "HEAD"
    )
    assert code == 2
    assert "--stub" in out


def test_plan_no_ai_uses_only_smoke_and_mapping(worlds, capsys):
    root, repo, _, _, _ = worlds
    make_suite(repo)
    config = str(root / "testagent.toml")
    code, out = _run(
        capsys,
        "plan",
        "--config",
        config,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--no-ai",
        "--run-id",
        "t5",
    )
    assert code == 0, out
    plan = json.loads((root / "artifacts" / "t5" / "plan.json").read_text(encoding="utf-8"))
    ids = [e["case_id"] for e in plan["entries"]]
    assert "TC-A" in ids
    assert any("未做影响分析" in u for u in plan["uncertain"])


def test_show_prints_report_and_json(worlds, capsys):
    root, repo, _, _, _ = worlds
    make_suite(repo)
    config = str(root / "testagent.toml")
    _run(
        capsys,
        "plan",
        "--config",
        config,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--stub",
        "--run-id",
        "t6",
    )
    _run(capsys, "run", "t6", "--config", config, "--env-version", "v", "--skip-confirm")
    code, out = _run(capsys, "show", "t6", "--config", config)
    assert code == 0
    assert "# 回归报告" in out
    code, out = _run(capsys, "show", "t6", "--config", config, "--json")
    assert code == 0
    assert json.loads(out)["state"] == "completed"


def test_check_reports_readiness(worlds, capsys):
    root, _, _, _, _ = worlds
    code, out = _run(capsys, "check", "--config", str(root / "testagent.toml"))
    assert code == 0, out
    assert "检查通过" in out
    assert "密钥环境变量" in out
