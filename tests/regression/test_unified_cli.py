"""Exercise routing and safety checks through the single installed CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_test_agent import cli


@pytest.mark.parametrize("config_first", [True, False])
def test_regression_config_routes_without_loading_exploration(
    worlds, monkeypatch, capsys, config_first
):
    root, *_ = worlds

    def forbidden_load(*args):
        pytest.fail("regression must not load exploration settings")

    monkeypatch.setattr(cli.Settings, "load", forbidden_load)
    config = ["--config", str(root / "testagent.toml")]
    args = [*config, "check"] if config_first else ["check", *config]
    assert cli.main(args) == 0
    assert "检查通过" in capsys.readouterr().out


def test_unified_plan_reconcile_and_dry_run(worlds, monkeypatch, capsys):
    root, *_ = worlds
    config = ["--config", str(root / "testagent.toml")]

    def forbidden_model(*args, **kwargs):
        pytest.fail("--stub must not construct a real model")

    monkeypatch.setattr(cli.regression_cli, "Analyzer", forbidden_model)
    assert (
        cli.main(
            [
                "plan",
                *config,
                "--base",
                "HEAD~1",
                "--head",
                "HEAD",
                "--stub",
                "--run-id",
                "unified",
                "--env-version",
                "local-test",
            ]
        )
        == 0
    )
    capsys.readouterr()
    plan_path = root / "artifacts/unified/plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert len(plan["entries"]) > 1
    plan["entries"] = plan["entries"][:1]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    run_args = ["run", "unified", *config, "--skip-confirm", "--dry-run"]
    assert cli.main(run_args) == 1
    assert "指纹" in capsys.readouterr().err
    assert cli.main(["reconcile", "unified", *config]) == 0
    capsys.readouterr()
    assert cli.main(run_args) == 0
    capsys.readouterr()
    record = json.loads((root / "artifacts/unified/run.json").read_text(encoding="utf-8"))
    assert record["results"]
    assert all(item["result"] == "skipped" for item in record["results"])
    assert cli.main(["show", "unified", *config]) == 0
    assert "unified" in capsys.readouterr().out
    assert cli.main(["runs", *config]) == 0
    assert "unified" in capsys.readouterr().out


def test_exploration_config_stays_separate(monkeypatch, capsys):
    seen = []
    sentinel = object()

    def load_config(path):
        seen.append(path)
        return sentinel

    monkeypatch.setattr(cli.Settings, "load", load_config)
    monkeypatch.setattr(
        cli.checks, "run_doctor", lambda settings: [] if settings is sentinel else None
    )
    assert cli.main(["--config", "exploration.toml", "doctor"]) == 0
    assert seen == [Path("exploration.toml")]
    capsys.readouterr()


def test_global_help_needs_no_configuration(monkeypatch, capsys):
    monkeypatch.setattr(cli.Settings, "load", lambda *_: pytest.fail("help must not load config"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "plan" in output and "agent" in output
    assert output.startswith("usage: testpilot")
    assert "uv run testagent" not in output
