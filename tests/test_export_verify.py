"""Export and verification against a throw-away Git repository (no target service involved)."""

import subprocess
from pathlib import Path

import pytest

from api_test_agent.layout import Layout
from api_test_agent.sandbox import export_target, git_read, verify_copy
from api_test_agent.settings import Settings

CRLF_BYTES = b"line one\r\nline two\r\n"


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    run_git(repo, "init", "-q")
    # Keep blobs byte-exact whatever the machine-wide autocrlf setting is.
    run_git(repo, "config", "core.autocrlf", "false")
    (repo / "plain.txt").write_text("hello\n", encoding="utf-8", newline="\n")
    (repo / "crlf.txt").write_bytes(CRLF_BYTES)
    (repo / "文档").mkdir()
    (repo / "文档" / "说明.md").write_text("中文\n", encoding="utf-8", newline="\n")
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    # Untracked local state that must never reach the copy.
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "real.db").write_bytes(b"real data")
    return repo, run_git(repo, "rev-parse", "HEAD")


def settings_for(tmp_path: Path, repo: Path, commit: str) -> Settings:
    return Settings.model_validate(
        {"target": {"source_repo": repo, "commit": commit}, "root": tmp_path / "project"}
    )


def test_export_is_byte_exact_and_leaves_untracked_files_behind(
    tmp_path: Path, source_repo: tuple[Path, str]
) -> None:
    repo, commit = source_repo
    settings = settings_for(tmp_path, repo, commit)
    dest = export_target(settings)
    assert (dest / "crlf.txt").read_bytes() == CRLF_BYTES
    assert (dest / "文档" / "说明.md").read_text(encoding="utf-8") == "中文\n"
    assert not (dest / ".env").exists()
    assert not (dest / "data").exists()
    report = verify_copy(settings)
    assert report.ok and report.expected_files == 3


def test_export_ignores_a_machine_wide_autocrlf_true(
    tmp_path: Path, source_repo: tuple[Path, str]
) -> None:
    repo, commit = source_repo
    run_git(repo, "config", "core.autocrlf", "true")  # what Git for Windows ships with
    settings = settings_for(tmp_path, repo, commit)
    export_target(settings)
    assert (Layout.from_settings(settings).sut_dir / "plain.txt").read_bytes() == b"hello\n"
    assert verify_copy(settings).ok


def test_verify_reports_modified_missing_and_extra_files(
    tmp_path: Path, source_repo: tuple[Path, str]
) -> None:
    repo, commit = source_repo
    settings = settings_for(tmp_path, repo, commit)
    dest = export_target(settings)
    (dest / "plain.txt").write_text("changed\n", encoding="utf-8")
    (dest / "crlf.txt").unlink()
    (dest / "stray.txt").write_text("x", encoding="utf-8")
    (dest / ".venv").mkdir()
    (dest / ".venv" / "ignored.txt").write_text("x", encoding="utf-8")
    report = verify_copy(settings)
    assert not report.ok
    assert report.modified == ("plain.txt",)
    assert report.missing == ("crlf.txt",)
    assert report.extra == ("stray.txt",)


def test_export_refuses_to_overwrite_without_force(
    tmp_path: Path, source_repo: tuple[Path, str]
) -> None:
    repo, commit = source_repo
    settings = settings_for(tmp_path, repo, commit)
    export_target(settings)
    with pytest.raises(Exception, match="already exists"):
        export_target(settings)
    export_target(settings, force=True)
    assert verify_copy(settings).ok


def test_export_does_not_touch_the_source_repository(
    tmp_path: Path, source_repo: tuple[Path, str]
) -> None:
    repo, commit = source_repo
    before = (git_read(repo, "rev-parse", "HEAD"), git_read(repo, "status", "--porcelain"))
    export_target(settings_for(tmp_path, repo, commit))
    after = (git_read(repo, "rev-parse", "HEAD"), git_read(repo, "status", "--porcelain"))
    assert before == after
