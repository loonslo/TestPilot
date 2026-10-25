from __future__ import annotations

from pathlib import Path

import pytest

from api_test_agent.regression.collect import CollectError, collect, is_excluded, resolve_version
from api_test_agent.regression.project import ProjectError, load_project


def test_collect_pins_both_ends_and_keeps_sources(worlds):
    _, repo, project, base, head = worlds
    context = collect(project, "HEAD~1", "HEAD")
    assert context.base_sha == base
    assert context.head_sha == head
    assert context.base_ref == "HEAD~1"
    paths = {c.path: c for c in context.changes}
    assert paths["src/orders/api.py"].status == "M"
    assert paths["src/orders/api.py"].modules == ["订单"]
    assert "库存不足" in paths["src/orders/api.py"].patch
    assert context.docs[0].path == "docs/orders.md"
    assert "金额上限" in context.docs[0].content


def test_collect_excludes_credentials_and_records_why(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    env = next(c for c in context.changes if c.path == ".env")
    assert env.excluded is True
    assert env.patch == ""
    assert "默认排除" in env.exclusion_reason
    assert ".env" not in context.analyzed_paths
    assert any("排除规则" in item for item in context.omissions)


def test_collect_rejects_same_commit_and_bad_refs(worlds):
    _, _, project, _, _ = worlds
    with pytest.raises(CollectError):
        collect(project, "HEAD", "HEAD")
    with pytest.raises(CollectError):
        collect(project, "no-such-ref", "HEAD")


def test_collect_warns_on_dirty_worktree(tmp_path):
    from conftest import commit, make_repo, write_project

    repo = make_repo(tmp_path)
    commit(repo, {"src/a.py": "x = 1\n"}, "init")
    commit(repo, {"src/a.py": "x = 2\n"}, "change")
    (repo / "src" / "a.py").write_text("x = 3\n", encoding="utf-8")
    project = load_project(write_project(tmp_path, repo))
    context = collect(project, "HEAD~1", "HEAD")
    assert any("未提交改动" in w for w in context.warnings)


def test_custom_redaction_pattern_applies(tmp_path):
    from conftest import commit, make_repo, write_project

    repo = make_repo(tmp_path)
    commit(repo, {"src/a.py": "x = 1\n", "data/dump.csv": "a,b\n"}, "init")
    commit(repo, {"src/a.py": "x = 2\n", "data/dump2.csv": "a,b\n"}, "change")
    config = write_project(
        tmp_path, repo, extra_toml='\n[[redaction]]\npattern = "\\\\.csv$"\nreason = "数据文件"\n'
    )
    project = load_project(config)
    context = collect(project, "HEAD~1", "HEAD")
    csv_change = next(c for c in context.changes if c.path.endswith(".csv"))
    assert csv_change.excluded
    assert "自定义排除规则" in csv_change.exclusion_reason


def test_budget_truncation_is_reported(tmp_path):
    from conftest import commit, make_repo, write_project

    repo = make_repo(tmp_path)
    commit(repo, {"src/orders/a.py": "x = 1\n"}, "init")
    big = "\n".join(f"line_{i} = {i}" for i in range(4000)) + "\n"
    commit(repo, {"src/orders/a.py": big}, "big change")
    config = write_project(tmp_path, repo, budget={"max_diff_chars": 1000})
    project = load_project(config)
    context = collect(project, "HEAD~1", "HEAD")
    change = next(c for c in context.changes if c.path == "src/orders/a.py")
    assert "截断" in change.patch
    assert any("预算" in item for item in context.omissions)


def test_module_miss_is_reported_as_gap_source(tmp_path):
    from conftest import commit, make_repo, write_project

    repo = make_repo(tmp_path)
    commit(repo, {"src/orders/a.py": "x = 1\n"}, "init")
    commit(repo, {"src/orders/a.py": "x = 2\n", "scripts/deploy.sh": "echo hi\n"}, "change")
    project = load_project(write_project(tmp_path, repo))
    context = collect(project, "HEAD~1", "HEAD")
    assert any(
        "scripts/deploy.sh" in item and "未命中模块映射" in item for item in context.omissions
    )


def test_is_excluded_covers_run_dirs_and_keys():
    assert is_excluded("runtime/cache.json", [])
    assert is_excluded("keys/server.pem", [])
    assert is_excluded("node_modules/x/index.js", [])
    assert is_excluded("src/app.py", []) is None


def test_resolve_version_rejects_option_injection(worlds):
    _, repo, _, _, _ = worlds
    with pytest.raises(CollectError):
        resolve_version(repo, "--help")


def test_missing_config_reports_file():
    with pytest.raises(ProjectError):
        load_project(Path("no-such-dir/testagent.toml"))


def test_smoke_must_reference_active_case(tmp_path):
    from conftest import commit, make_repo, write_project

    repo = make_repo(tmp_path)
    commit(repo, {"src/a.py": "x = 1\n"}, "init")
    config = write_project(tmp_path, repo, smoke=("TC-NOT-EXIST",))
    with pytest.raises(ProjectError) as info:
        load_project(config)
    assert "TC-NOT-EXIST" in str(info.value)


def test_yaml_failure_message_is_actionable(tmp_path):
    from conftest import commit, make_repo, write_project

    repo = make_repo(tmp_path)
    commit(repo, {"src/a.py": "x = 1\n"}, "init")
    config = write_project(tmp_path, repo)
    (tmp_path / "assets" / "cases.yaml").write_text("cases: [\n", encoding="utf-8")
    with pytest.raises(ProjectError) as info:
        load_project(config)
    assert "cases.yaml" in str(info.value)
