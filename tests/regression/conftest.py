"""测试公共夹具：临时 git 仓库 + 项目配置 + 资产。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from api_test_agent.regression.project import Project, load_project


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "core.autocrlf", "false")
    return repo


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def write_project(
    root: Path,
    repo: Path,
    *,
    smoke=("TC-A",),
    budget: dict[str, int] | None = None,
    extra_toml: str = "",
) -> Path:
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "orders.md").write_text(
        "# 订单业务规则\n\n创建订单要求库存充足，金额上限 10000 元。\n", encoding="utf-8"
    )
    (root / "assets" / "modules.yaml").write_text(
        """
modules:
  - module: 订单
    summary: 下单与库存扣减
    paths: [src/orders]
    keywords: [订单]
    cases: [TC-A]
  - module: 用户
    summary: 用户资料
    paths: [src/users]
""",
        encoding="utf-8",
    )
    (root / "assets" / "cases.yaml").write_text(
        """
cases:
  - id: TC-A
    name: 创建订单成功
    module: 订单
    entry: [tests/test_orders.py::test_create_ok]
    status: active
  - id: TC-B
    name: 库存不足报错
    module: 订单
    entry: [tests/test_orders.py::test_stock_low]
    status: active
  - id: TC-UI
    name: 下单页面
    module: 订单
    entry: [tests/test_orders.py::test_create_ok]
    status: ui
""",
        encoding="utf-8",
    )
    smoke_lines = "\n".join(f"  - {case_id}" for case_id in smoke)
    (root / "assets" / "smoke.yaml").write_text(f"smoke:\n{smoke_lines}\n", encoding="utf-8")
    config = f"""
name = "演示项目"
repository = "{repo.as_posix()}"
docs = ["docs/orders.md"]
modules_file = "assets/modules.yaml"
cases_file = "assets/cases.yaml"
smoke_file = "assets/smoke.yaml"
artifacts_dir = "artifacts"

[model]
base_url = "http://127.0.0.1:9999/v1"
model = "fake-model"
api_key_env = "TESTAGENT_FAKE_KEY"
max_attempts = 2

[budget]
max_changed_files = {budget.get("max_changed_files", 20) if budget else 20}
max_diff_chars = {budget.get("max_diff_chars", 50000) if budget else 50000}
"""
    config += extra_toml
    path = root / "testagent.toml"
    path.write_text(config, encoding="utf-8")
    return path


@pytest.fixture
def worlds(tmp_path: Path):
    """返回 (项目根目录, 仓库目录, 项目对象, base_sha, head_sha)。"""

    repo = make_repo(tmp_path)
    base = commit(
        repo,
        {
            "src/orders/api.py": "def create_order(qty):\n    return {'ok': True}\n",
            "src/users/profile.py": "def profile():\n    return {}\n",
            "tests/test_orders.py": "",
        },
        "init",
    )
    head = commit(
        repo,
        {
            "src/orders/api.py": (
                "def create_order(qty):\n"
                "    if qty > 10:\n"
                "        raise ValueError('库存不足')\n"
                "    return {'ok': True}\n"
            ),
            ".env": "SECRET=top-secret-value\n",
        },
        "feat: 下单库存校验",
    )
    config_path = write_project(tmp_path, repo)
    project = load_project(config_path)
    return tmp_path, repo, project, base, head


def make_suite(repo: Path) -> None:
    """在仓库里放一个真实可跑的 pytest 文件，用于执行链路测试。"""

    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_orders.py").write_text(
        "def test_create_ok():\n    assert True\n\n\n"
        "def test_stock_low():\n    assert 1 + 1 == 3\n",
        encoding="utf-8",
    )


@pytest.fixture
def project(worlds) -> Project:
    return worlds[2]
