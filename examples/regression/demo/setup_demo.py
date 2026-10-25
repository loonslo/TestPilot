"""生成一个自包含的演示仓库，用于在没有真实项目和模型密钥时验证全链路。

用法（在仓库根目录执行）：
    uv run python examples/regression/demo/setup_demo.py
    cd examples/regression/demo
    uv run testagent plan --base base --head head --stub --env-version demo
    uv run testagent run <run_id> --skip-confirm

脚本只会创建 examples/regression/demo/_repo 与 assets；重复执行会重建演示仓库（仅限该目录）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE / "_repo"

BASE_FILES = {
    "src/cart/service.py": (
        "def total(items, coupon=None):\n"
        "    amount = sum(item['price'] * item['qty'] for item in items)\n"
        "    return amount\n"
    ),
    "src/cart/api.py": (
        "from .service import total\n\n\n"
        "def checkout(items):\n"
        "    return {'amount': total(items)}\n"
    ),
    "tests/test_cart.py": (
        "from src.cart.service import total\n\n\n"
        "def test_total_without_coupon():\n"
        "    assert total([{'price': 10, 'qty': 2}]) == 20\n"
    ),
}

HEAD_FILES = {
    "src/cart/service.py": (
        "def total(items, coupon=None):\n"
        "    amount = sum(item['price'] * item['qty'] for item in items)\n"
        "    if coupon:\n"
        "        if amount < coupon['min_amount']:\n"
        "            raise ValueError('未达到优惠券门槛')\n"
        "        amount = amount - coupon['amount']\n"
        "    return max(amount, 0)\n"
    ),
    "src/cart/api.py": (
        "from .service import total\n\n\n"
        "def checkout(items, coupon=None):\n"
        "    return {'amount': total(items, coupon)}\n"
    ),
}


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(REPO), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
    return proc.stdout.strip()


def write(files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = REPO / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    if REPO.exists():
        shutil.rmtree(REPO)
    REPO.mkdir(parents=True)
    git("init", "-q")
    git("config", "user.email", "demo@example.com")
    git("config", "user.name", "demo")
    git("config", "core.autocrlf", "false")
    git("checkout", "-q", "-b", "main")
    write(BASE_FILES)
    git("add", "-A")
    git("commit", "-q", "-m", "feat: 购物车下单")
    git("tag", "base")
    write(HEAD_FILES)
    write(
        {
            "tests/test_cart.py": (
                "from src.cart.service import total\n\n\n"
                "def test_total_without_coupon():\n"
                "    assert total([{'price': 10, 'qty': 2}]) == 20\n\n\n"
                "def test_total_with_coupon():\n"
                "    assert total([{'price': 10, 'qty': 2}], {'amount': 5, 'min_amount': 10}) == 15\n\n\n"
                "def test_coupon_threshold_rejected():\n"
                "    try:\n"
                "        total([{'price': 1, 'qty': 1}], {'amount': 5, 'min_amount': 100})\n"
                "    except ValueError:\n"
                "        return\n"
                "    raise AssertionError('低于门槛时应报错')\n"
            )
        }
    )
    git("add", "-A")
    git("commit", "-q", "-m", "feat: 购物车支持优惠券门槛")
    git("tag", "head")
    print(f"演示仓库已生成: {REPO}")
    print("下一步（在本目录执行）：")
    print("  uv run testagent plan --base base --head head --stub --env-version demo")
    print("  uv run testagent run <run_id> --skip-confirm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
