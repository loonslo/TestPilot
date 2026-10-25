import subprocess
from pathlib import Path

import pytest

from api_test_agent.layout import MARKER, SandboxError, remove_managed_tree, validate_id


@pytest.mark.parametrize("value", ["a", "seed-v1", "check-1", "0abc", "a" * 40])
def test_valid_ids(value: str) -> None:
    assert validate_id(value, "id") == value


@pytest.mark.parametrize("value", ["", "-a", "A", "a_b", "a/b", "..", "a b", "a" * 41, "中文"])
def test_invalid_ids(value: str) -> None:
    with pytest.raises(SandboxError):
        validate_id(value, "id")


def make_managed(parent: Path, name: str, *, marker: bool = True) -> Path:
    path = parent / name
    path.mkdir(parents=True)
    (path / "file.txt").write_text("x", encoding="utf-8")
    if marker:
        (path / MARKER).write_text("m", encoding="utf-8")
    return path


def test_removes_a_marked_directory_directly_inside_the_parent(tmp_path: Path) -> None:
    parent = tmp_path / "scenarios"
    target = make_managed(parent, "run-1")
    remove_managed_tree(target, parent, require_marker=True)
    assert not target.exists()
    assert parent.exists()


def test_refuses_without_marker(tmp_path: Path) -> None:
    parent = tmp_path / "scenarios"
    target = make_managed(parent, "run-1", marker=False)
    with pytest.raises(SandboxError, match="marker|not created"):
        remove_managed_tree(target, parent, require_marker=True)
    assert target.exists()


def test_refuses_a_directory_outside_the_parent(tmp_path: Path) -> None:
    parent = tmp_path / "scenarios"
    parent.mkdir()
    outside = make_managed(tmp_path / "elsewhere", "run-1")
    with pytest.raises(SandboxError, match="outside"):
        remove_managed_tree(outside, parent, require_marker=True)
    assert outside.exists()


def test_refuses_a_grandchild(tmp_path: Path) -> None:
    parent = tmp_path / "scenarios"
    nested = make_managed(parent / "group", "run-1")
    with pytest.raises(SandboxError, match="outside"):
        remove_managed_tree(nested, parent, require_marker=True)


def test_refuses_a_junction_and_keeps_its_target(tmp_path: Path) -> None:
    parent = tmp_path / "scenarios"
    parent.mkdir()
    real = make_managed(tmp_path / "real", "data")
    link = parent / "run-1"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, check=False
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    with pytest.raises(SandboxError, match="link"):
        remove_managed_tree(link, parent, require_marker=True)
    assert (real / "file.txt").exists()
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, check=False)
