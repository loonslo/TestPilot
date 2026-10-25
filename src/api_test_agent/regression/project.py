"""project 模块：加载项目配置、业务资料、模块映射、用例目录与冒烟集。

只读，不修改被分析项目。所有相对路径基于配置文件所在目录解析；凭据不进入配置与模型上下文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from api_test_agent.regression.models import (
    CaseCatalog,
    DocRef,
    ModuleEntry,
    ModuleMap,
    ProjectConfig,
    TestCase,
)

CONFIG_NAMES = ("testagent.toml", "project.toml")
MODULE_FIELDS = ("module", "summary", "paths", "keywords", "api_paths", "cases")


class ProjectError(RuntimeError):
    """配置或资产错误。消息面向使用者，指出具体文件与字段。"""


@dataclass(frozen=True)
class Project:
    config: ProjectConfig
    root: Path
    repo: Path
    module_map: ModuleMap
    catalog: CaseCatalog
    warnings: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def artifacts_dir(self) -> Path:
        return resolve_path(self.root, self.config.artifacts_dir)

    @property
    def prompts_dir(self) -> Path:
        if self.config.prompts_dir:
            return resolve_path(self.root, self.config.prompts_dir)
        return Path(__file__).resolve().parent / "prompts"

    def cases_by_id(self) -> dict[str, TestCase]:
        return {c.id: c for c in self.catalog.cases}


def resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def find_config(explicit: Path | None, project_dir: Path | None = None) -> Path:
    if explicit is not None:
        path = explicit.resolve()
        if not path.is_file():
            raise ProjectError(f"找不到项目配置: {path}")
        return path
    base = (project_dir or Path.cwd()).resolve()
    for name in CONFIG_NAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate
    raise ProjectError(f"{base} 下没有 {' 或 '.join(CONFIG_NAMES)}；可用 --config 指定")


def format_validation(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def _load_toml(path: Path) -> dict[str, Any]:
    import tomllib

    if not path.is_file():
        raise ProjectError(f"找不到配置文件: {path}")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"{path.name} TOML 解析失败: {exc}") from exc


def load_config(path: Path) -> ProjectConfig:
    data = _load_toml(path)
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ProjectError(f"{path.name} 配置校验失败:\n{format_validation(exc)}") from exc


def _load_structured(path: Path) -> dict[str, Any]:
    """读取 YAML（需要 PyYAML）或 JSON，统一返回 dict。"""

    if not path.is_file():
        raise ProjectError(f"找不到资产文件: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProjectError(f"{path.name} JSON 解析失败: {exc}") from exc
    else:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            raise ProjectError(
                f"{path.name} 是 YAML，但当前环境没有 PyYAML。"
                "执行 pip install pyyaml，或把资产改为 .json 并在配置中引用。"
            ) from None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:  # pragma: no cover - 依赖第三方解析器
            raise ProjectError(f"{path.name} YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError(f"{path.name} 顶层必须是映射")
    return data


def load_modules(path: Path) -> tuple[ModuleMap, list[str]]:
    data = _load_structured(path)
    raw = data.get("modules") or []
    if not isinstance(raw, list):
        raise ProjectError(f"{path.name}: modules 必须是列表")
    entries: list[ModuleEntry] = []
    warnings: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ProjectError(f"{path.name}: modules[{index}] 必须是映射")
        if not str(item.get("module") or "").strip():
            raise ProjectError(f"{path.name}: modules[{index}] 缺少 module 字段")
        extra = sorted(set(item) - set(MODULE_FIELDS))
        if extra:
            warnings.append(f"模块 {item['module']}: 忽略未知字段 {', '.join(extra)}")
        try:
            entries.append(
                ModuleEntry.model_validate({k: v for k, v in item.items() if k in MODULE_FIELDS})
            )
        except ValidationError as exc:
            raise ProjectError(
                f"{path.name}: modules[{index}] 校验失败\n{format_validation(exc)}"
            ) from exc
    names = [e.module for e in entries]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ProjectError(f"{path.name}: 模块名重复 {sorted(dupes)}")
    return ModuleMap(modules=entries), warnings


def load_catalog(cases_path: Path, smoke_path: Path) -> CaseCatalog:
    data = _load_structured(cases_path)
    raw_cases = data.get("cases") or []
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ProjectError(f"{cases_path.name}: cases 必须是非空列表")
    cases: list[TestCase] = []
    for index, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise ProjectError(f"{cases_path.name}: cases[{index}] 必须是映射")
        try:
            cases.append(TestCase.model_validate(item))
        except ValidationError as exc:
            raise ProjectError(
                f"{cases_path.name}: 用例 {item.get('id', f'#{index}')} 校验失败\n{format_validation(exc)}"
            ) from exc
    smoke: list[str] = []
    if smoke_path.is_file():
        smoke_data = _load_structured(smoke_path)
        raw_smoke = smoke_data.get("smoke") or []
        if not isinstance(raw_smoke, list):
            raise ProjectError(f"{smoke_path.name}: smoke 必须是列表")
        smoke = [str(x) for x in raw_smoke]
    try:
        return CaseCatalog(cases=cases, smoke=smoke)
    except ValidationError as exc:
        raise ProjectError(f"用例目录校验失败\n{format_validation(exc)}") from exc


def markdown_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        return ""
    return ""


def openapi_meta(text: str) -> tuple[str, str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "", ""
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return "", ""
    return str(info.get("title", "")), str(info.get("version", ""))


def load_docs(config: ProjectConfig, root: Path, char_limit: int) -> list[DocRef]:
    refs: list[DocRef] = []
    used = 0
    for relative in config.docs:
        resolved = resolve_path(root, relative)
        is_openapi = resolved.suffix.lower() in {".json", ".yaml", ".yml"}
        kind: str = "openapi" if is_openapi else "markdown"
        if not resolved.is_file():
            refs.append(DocRef(path=relative, kind=kind, missing=True))  # type: ignore[arg-type]
            continue
        text = resolved.read_text(encoding="utf-8", errors="replace")
        title, version = (
            openapi_meta(text) if is_openapi else (markdown_title(text) or resolved.stem, "")
        )
        budget = max(0, char_limit - used)
        content = text[:budget]
        refs.append(
            DocRef(
                path=relative,
                kind=kind,  # type: ignore[arg-type]
                title=title,
                version=version,
                content=content,
                char_count=len(text),
                truncated=len(content) < len(text),
            )
        )
        used += len(content)
    return refs


def load_project(config_path: Path) -> Project:
    config = load_config(config_path)
    root = config_path.resolve().parent
    repo = resolve_path(root, config.repository)
    if not repo.is_dir():
        raise ProjectError(f"repository 路径不存在: {repo}")
    module_map, warnings = load_modules(resolve_path(root, config.modules_file))
    catalog = load_catalog(
        resolve_path(root, config.cases_file), resolve_path(root, config.smoke_file)
    )
    if not catalog.smoke:
        warnings.append("冒烟集为空：每次回归将没有强制的核心路径用例")
    return Project(
        config=config,
        root=root,
        repo=repo,
        module_map=module_map,
        catalog=catalog,
        warnings=tuple(warnings),
    )


def catalog_overview(catalog: CaseCatalog) -> list[dict[str, Any]]:
    """给模型看的用例目录摘要：只含编号、名称、模块、接口与状态。"""

    return [
        {
            "id": case.id,
            "name": case.name,
            "module": case.module,
            "api": case.api,
            "status": case.status,
        }
        for case in catalog.cases
    ]
