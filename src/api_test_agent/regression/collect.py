"""collect 模块：固定 base/head 版本，读取变更列表、diff 与必要上下文。

原则：
- 版本两端固定为提交 SHA，并在采集时记录，分析中途代码被改动也不影响本次结果。
- 排除凭据与运行目录，不做整仓无差别上传；超预算时缩小范围并记录未分析部分。
- 每条上下文都带来源路径，供报告核对。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from api_test_agent.regression.models import (
    CollectionContext,
    ContextFile,
    DocRef,
    FileChange,
    ModuleEntry,
)
from api_test_agent.regression.project import Project, catalog_overview, load_docs

# 默认排除：凭据、环境文件、依赖、构建产物、运行目录与常见二进制
DEFAULT_EXCLUDES: tuple[str, ...] = (
    r"(^|/)\.env(\..+)?$",
    r"(^|/)\.git/",
    r"(^|/)(node_modules|\.venv|venv|__pycache__|\.mypy_cache|\.pytest_cache|\.ruff_cache)/",
    r"(^|/)(dist|build|target|out|coverage|htmlcov)/",
    r"(^|/)(runtime|tmp|temp|logs|log|artifacts|\.cache)/",
    r"(^|/)(\.idea|\.vscode)/",
    r"\.(pem|key|p12|pfx|jks|keystore|kdbx)$",
    r"(^|/)(id_rsa|id_ed25519|\.netrc|\.npmrc|\.pypirc|credentials\.json|secrets\.json|service[-_]account\.json)$",
    r"\.(png|jpe?g|gif|ico|webp|pdf|zip|gz|7z|rar|mp4|mov|onnx|pt|pth|bin|jar|class|exe|dll|so|dylib|woff2?|ttf)$",
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|poetry\.lock|uv\.lock)$",
)

TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".kt",
    ".go",
    ".rb",
    ".php",
    ".cs",
    ".rs",
    ".scala",
    ".swift",
    ".mjs",
    ".cjs",
    ".vue",
    ".sql",
    ".proto",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".json",
    ".md",
    ".txt",
    ".graphql",
    ".sh",
    ".ps1",
    ".bat",
    ".css",
    ".scss",
    ".html",
    ".xml",
    ".gradle",
    ".kts",
    ".properties",
}
TEST_HINTS = ("test_", "_test.", "/tests/", "/test/", ".spec.", ".test.", "__tests__")
MAX_PATCH_CHARS_PER_FILE = 40_000
MAX_LINE_HINTS = 25


class CollectError(RuntimeError):
    """采集失败：仓库不可用、版本不存在、路径逃逸等。"""


@dataclass(frozen=True)
class Version:
    ref: str
    sha: str
    subject: str


def _run_git(repo: Path, *args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CollectError("找不到 git 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectError(f"git {' '.join(args)} 超时") from exc


def _git(repo: Path, *args: str) -> str:
    proc = _run_git(repo, *args)
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise CollectError(f"git {' '.join(args)} 失败: {lines[0] if lines else '未知错误'}")
    return proc.stdout


def ensure_repository(repo: Path) -> None:
    if not repo.is_dir():
        raise CollectError(f"仓库路径不存在: {repo}")
    proc = _run_git(repo, "rev-parse", "--git-dir")
    if proc.returncode != 0:
        raise CollectError(f"{repo} 不是 Git 仓库或无权访问")


def resolve_version(repo: Path, ref: str) -> Version:
    if not ref or ref.startswith("-") or " " in ref:
        raise CollectError(f"非法版本引用: {ref!r}")
    proc = _run_git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    if proc.returncode != 0:
        raise CollectError(f"版本 {ref!r} 不存在于 {repo}")
    sha = proc.stdout.strip()
    subject = _git(repo, "log", "-1", "--format=%s", sha).strip()
    return Version(ref=ref, sha=sha, subject=subject)


def merge_base(repo: Path, base_sha: str, head_sha: str) -> str:
    proc = _run_git(repo, "merge-base", base_sha, head_sha)
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else base_sha


def is_ancestor(repo: Path, base_sha: str, head_sha: str) -> bool:
    return _run_git(repo, "merge-base", "--is-ancestor", base_sha, head_sha).returncode == 0


def verify_commits(repo: Path, shas: list[str]) -> None:
    """执行或报告前复核：相关提交对象仍然存在。

    只校验对象可达，不比对工作区内容；工作区是否有未提交改动由 collect 记录为警告。
    """

    for sha in shas:
        proc = _run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        if proc.returncode != 0:
            raise CollectError(f"提交 {sha[:12]} 已不可达")


def is_excluded(path: str, extra: list[str]) -> str | None:
    for pattern in extra:
        if re.search(pattern, path):
            return f"自定义排除规则 {pattern}"
    for pattern in DEFAULT_EXCLUDES:
        if re.search(pattern, path):
            return f"默认排除规则 {pattern}"
    return None


def _numstat(repo: Path, base: str, head: str) -> dict[str, tuple[int, int, bool]]:
    out = _git(repo, "diff", "--numstat", "-z", "-M", base, head)
    stats: dict[str, tuple[int, int, bool]] = {}
    fields = out.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        parts = entry.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        binary = added == "-" and deleted == "-"
        values = (0 if binary else int(added or 0), 0 if binary else int(deleted or 0), binary)
        if not path and index < len(fields):  # 重命名：新路径在下一条
            path = fields[index]
            index += 1
        stats[path] = values
    return stats


def _name_status(repo: Path, base: str, head: str) -> list[tuple[str, str, str | None]]:
    out = _git(repo, "diff", "--name-status", "-z", "-M", base, head)
    tokens = [t for t in out.split("\0") if t != ""]
    result: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if status[0] in "RC":
            if index + 1 >= len(tokens):
                break
            result.append((status[0], tokens[index + 1], tokens[index]))
            index += 2
        else:
            if index >= len(tokens):
                break
            result.append((status[0], tokens[index], None))
            index += 1
    return result


def _patch(repo: Path, base: str, head: str, path: str, old_path: str | None) -> str:
    args = ["diff", "--no-color", "--unified=3", base, head, "--"]
    if old_path:
        args.append(old_path)
    args.append(path)
    text = _git(repo, *args)
    if len(text) > MAX_PATCH_CHARS_PER_FILE:
        # 按行边界截断，绝不能在 hunk 头处切断（否则会丢掉整个文件的正文）
        cut = text.rfind("\n", 0, MAX_PATCH_CHARS_PER_FILE)
        text = text[: cut if cut > 0 else MAX_PATCH_CHARS_PER_FILE]
        text += f"\n...（该文件 diff 过长，已截断到约 {MAX_PATCH_CHARS_PER_FILE} 字符，后续 hunk 未展示）"
    return text


def line_hints(patch: str) -> list[str]:
    """从 unified diff 头部提取被改动的行号范围，供报告定位。"""

    hints: list[str] = []
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
        elif line.startswith("@@") and current:
            match = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or 1)
                hints.append(f"{current}:{start}-{start + max(count, 1) - 1}")
                if len(hints) >= MAX_LINE_HINTS:
                    break
    return hints


def is_text(path: str) -> bool:
    return Path(path).suffix.lower() in TEXT_SUFFIXES


def is_test_path(path: str) -> bool:
    lowered = "/" + path.lower()
    return any(hint in lowered for hint in TEST_HINTS)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...（已截断，原文 {len(text)} 字符）", True


def _read_repo_file(repo: Path, path: str, limit: int) -> tuple[str, bool] | None:
    target = (repo / path).resolve()
    try:
        target.relative_to(repo.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    return _truncate(target.read_text(encoding="utf-8", errors="replace"), limit)


def _modules_hit(project: Project, changes: list[FileChange]) -> list[ModuleEntry]:
    seen: dict[str, ModuleEntry] = {}
    for change in changes:
        if change.excluded and change.exclusion_reason.startswith(("默认排除", "自定义排除")):
            continue
        for entry in project.module_map.by_path(change.path):
            seen.setdefault(entry.module, entry)
    return list(seen.values())


def _covered_modules(project: Project) -> set[str]:
    return {c.module for c in project.catalog.active() if c.module}


def collect(
    project: Project,
    base_ref: str,
    head_ref: str,
    *,
    extra_excludes: list[str] | None = None,
) -> CollectionContext:
    budget = project.config.budget
    repo = project.repo
    ensure_repository(repo)
    base = resolve_version(repo, base_ref)
    head = resolve_version(repo, head_ref)
    if base.sha == head.sha:
        raise CollectError("base 与 head 指向同一个提交，没有可分析的变更")
    if not is_ancestor(repo, base.sha, head.sha):
        raise CollectError(
            f"{base.ref}（{base.sha[:12]}）不是 {head.ref}（{head.sha[:12]}）的祖先。"
            "首版只比较发布版本两端的顺序快照，请确认两个提交在同一条线上"
        )
    extra = list(extra_excludes or []) + [rule.pattern for rule in project.config.redaction]
    stats = _numstat(repo, base.sha, head.sha)
    statuses = _name_status(repo, base.sha, head.sha)

    omissions: list[str] = []
    changes: list[FileChange] = []
    diff_used = 0
    diff_total = 0
    kept = 0
    excluded_count = 0
    over_budget = 0
    for status, path, old_path in statuses:
        added, deleted, binary = stats.get(path, (0, 0, False))
        change = FileChange(
            path=path,
            status=status,
            old_path=old_path,
            added=added,
            deleted=deleted,
            binary=binary,
            modules=[e.module for e in project.module_map.by_path(path)],
        )
        reason = is_excluded(path, extra)
        if reason:
            change.excluded = True
            change.exclusion_reason = reason
            excluded_count += 1
            changes.append(change)
            continue
        if binary or not is_text(path):
            change.binary = binary or not is_text(path)
            change.exclusion_reason = "二进制或非文本文件，只列入文件列表"
            changes.append(change)
            continue
        if kept >= budget.max_changed_files:
            change.excluded = True
            change.exclusion_reason = f"超出文件数预算 {budget.max_changed_files}"
            over_budget += 1
            changes.append(change)
            continue
        patch = _patch(repo, base.sha, head.sha, path, old_path)
        diff_total += len(patch)
        remaining = budget.max_diff_chars - diff_used
        if remaining <= 0:
            change.excluded = True
            change.exclusion_reason = f"超出 diff 字符预算 {budget.max_diff_chars}"
            over_budget += 1
            changes.append(change)
            continue
        truncated = False
        if len(patch) > remaining:
            patch, truncated = _truncate(patch, remaining)
            over_budget += 1
        if truncated:
            omissions.append(f"{path}: diff 超出剩余预算，只发送了开头部分")
        diff_used += len(patch)
        change.patch = patch
        kept += 1
        changes.append(change)

    if excluded_count:
        omissions.append(
            f"{excluded_count} 个文件按排除规则未送入模型（凭据、依赖、构建产物、运行目录等）"
        )
    if over_budget:
        omissions.append(
            f"{over_budget} 处因输入预算被截断或跳过（max_changed_files={budget.max_changed_files}，"
            f"max_diff_chars={budget.max_diff_chars}）；未分析部分已在文件表中标出"
        )

    hit_modules = _modules_hit(project, changes)
    for entry in hit_modules:
        if not any(c.module == entry.module for c in project.catalog.active()):
            omissions.append(
                f"模块 {entry.module} 在本次变更中命中，但用例目录没有它的 active 用例"
            )
    mapped_prefixes = [p for e in project.module_map.modules for p in e.paths]
    for change in changes:
        if change.excluded or change.modules:
            continue
        if not any(
            change.path == p or change.path.startswith(p.rstrip("/") + "/") for p in mapped_prefixes
        ):
            omissions.append(f"{change.path} 未命中模块映射，业务归属待确认")

    committed_paths = _git(repo, "ls-tree", "-r", "--name-only", head.sha).splitlines()
    context_files: list[ContextFile] = []
    remaining_context = budget.max_file_context_files
    for entry in hit_modules:
        if remaining_context <= 0:
            break
        for prefix in entry.paths:
            for rel in sorted(committed_paths):
                if remaining_context <= 0:
                    break
                if not (rel == prefix or rel.startswith(prefix.rstrip("/") + "/")):
                    continue
                if rel in {c.path for c in changes} or is_excluded(rel, extra) or not is_text(rel):
                    continue
                if rel in {f.path for f in context_files}:
                    continue
                content = _git(repo, "show", f"{head.sha}:{rel}")
                truncated = len(content) > budget.max_file_context_chars
                content = content[: budget.max_file_context_chars]
                context_files.append(
                    ContextFile(
                        path=rel,
                        source="head_snapshot",
                        content=content,
                        truncated=truncated,
                        reason=f"模块映射 {entry.module} 的相关文件（本次未改动，供理解调用关系）",
                    )
                )
                remaining_context -= 1

    docs: list[DocRef] = load_docs(project.config, project.root, budget.max_doc_chars)
    for doc in docs:
        if doc.missing:
            omissions.append(f"业务资料缺失，未纳入分析: {doc.path}")
        elif doc.truncated:
            omissions.append(
                f"业务资料 {doc.path} 超出文档预算，只发送了前 {len(doc.content)} 字符"
            )

    warnings = list(project.warnings)
    dirty = _git(repo, "status", "--porcelain").strip()
    if dirty:
        warnings.append(
            f"仓库有未提交改动（{len(dirty.splitlines())} 项）：本次分析与计划只代表 {head.sha[:12]} 的内容，"
            "执行前请确认工作区与 head 提交一致"
        )

    context = CollectionContext(
        project=project.name,
        repository=str(repo),
        base_sha=base.sha,
        head_sha=head.sha,
        base_ref=base.ref,
        head_ref=head.ref,
        merge_base=merge_base(repo, base.sha, head.sha),
        subject=f"{base.subject} -> {head.subject}",
        changes=changes,
        context_files=context_files,
        docs=docs,
        modules=project.module_map.modules,
        cases_overview=catalog_overview(project.catalog),
        omissions=omissions,
        warnings=warnings,
        max_prompt_chars=budget.max_total_prompt_chars,
        diff_total_chars=diff_total,
        diff_included_chars=diff_used,
    )
    # The final serializer enforces this limit including template and JSON overhead.
    return context
