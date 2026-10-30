"""结构化数据契约：配置、采集上下文、影响分析、回归计划、运行记录与报告。

约定：
- 所有跨模块传递的数据都经过 Pydantic 校验，禁止 dict 裸传。
- analyze 只产出 ``ImpactAnalysis``；用例编号是否真实存在由 plan 校验，模型无权决定。
- 测试结果只来自 runner 的真实执行；report 不补写缺失结果。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- 状态


TaskState = Literal["planned", "running", "completed", "aborted", "incomplete"]
"""任务流程状态（与测试结果分开记录）。

- planned    已生成计划，等待人工确认
- running    执行中
- completed  全流程完成（该状态不代表测试通过）
- aborted    执行异常中断（超时/进程异常/配置错误）
- incomplete 未完成（被中断或部分用例未能解析）
"""

CaseResult = Literal["passed", "failed", "error", "skipped"]
"""单条用例的测试结果。error 表示执行该用例时出现异常，不计为通过。"""

EvidenceKind = Literal["direct", "inferred", "uncertain"]
"""影响证据强度：direct=直接证据，inferred=关联推断，uncertain=待确认。"""

CasePriority = Literal["smoke", "model", "manual"]
"""用例进入计划的来源：smoke=固定冒烟集，model=模型建议且人已确认，manual=人工追加。"""


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- 配置


class RedactionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: str = Field(min_length=1, description="正则；命中内容在送模型前替换为 [REDACTED]")
    reason: str = ""


class ModelConfig(BaseModel):
    """一个已经验证可用的模型接口，不做模型路由。SDK 自身重试关闭，由本程序控制。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(default="https://api.openai.com/v1", min_length=1)
    model: str = Field(min_length=1, description="模型标识")
    api_key_env: str = Field(default="TESTAGENT_MODEL_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    max_output_tokens: int = Field(default=8000, gt=0, le=65536)
    temperature: float = Field(default=0.1, ge=0, le=2)
    max_attempts: int = Field(default=2, gt=0, le=5, description="可重试错误的尝试次数")
    json_mode: bool = Field(default=True, description="请求 response_format=json_object")


class BudgetConfig(BaseModel):
    """一次 plan 的输入预算。超出时缩小分析范围并明确记录未分析部分。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_changed_files: int = Field(default=150, gt=0, le=2000)
    max_diff_chars: int = Field(default=400_000, gt=0)
    max_file_context_chars: int = Field(default=5000, gt=100, le=100_000)
    max_file_context_files: int = Field(default=25, gt=0, le=200)
    max_doc_chars: int = Field(default=60_000, gt=0)
    max_total_prompt_chars: int = Field(default=600_000, gt=0)


class SuiteConfig(BaseModel):
    """既有接口测试框架的执行方式（预先配置，runner 不得自行拼接系统命令之外的东西）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    python: str = Field(default="", description="执行测试的解释器；空表示当前解释器")
    timeout_seconds: float = Field(default=600.0, gt=0, le=7200)
    extra_args: list[str] = Field(default_factory=list, description="固定附加给 pytest 的参数")


class ProjectConfig(BaseModel):
    """项目配置；相对路径基于配置文件所在目录解析。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    repository: str = Field(min_length=1, description="本地 Git 仓库路径")
    docs: list[str] = Field(
        default_factory=list, description="团队同意共享的业务资料（Markdown/OpenAPI）"
    )
    modules_file: str = "assets/modules.yaml"
    cases_file: str = "assets/cases.yaml"
    smoke_file: str = "assets/smoke.yaml"
    prompts_dir: str | None = None
    artifacts_dir: str = "artifacts"
    suite: SuiteConfig = SuiteConfig()
    model: ModelConfig | None = None
    budget: BudgetConfig = BudgetConfig()
    redaction: list[RedactionRule] = Field(default_factory=list)
    environment: dict[str, str] = Field(
        default_factory=dict, description="附加到测试进程的环境变量（不含凭据）"
    )

    @model_validator(mode="after")
    def _check_docs(self) -> ProjectConfig:
        for doc in self.docs:
            if not doc.strip():
                raise ValueError("docs 中存在空路径")
        return self

    @field_validator("redaction")
    @classmethod
    def _compile_redaction(cls, rules: list[RedactionRule]) -> list[RedactionRule]:
        import re

        for rule in rules:
            try:
                re.compile(rule.pattern)
            except re.error as exc:  # pragma: no cover - 配置错误路径
                raise ValueError(f"redaction 正则无效 {rule.pattern!r}: {exc}") from exc
        return rules


# --------------------------------------------------------------------------- 资产


class ModuleEntry(BaseModel):
    """模块映射的一行：代码目录/接口 -> 业务模块。人工维护，只做精确前缀与路径匹配。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str = Field(min_length=1)
    summary: str = ""
    paths: list[str] = Field(
        default_factory=list, description="仓库相对路径前缀；空表示按键值/接口匹配"
    )
    keywords: list[str] = Field(default_factory=list, description="业务关键词，用于粗粒度关联")
    api_paths: list[str] = Field(
        default_factory=list, description="关联的接口路径前缀，如 /api/v1/orders"
    )
    cases: list[str] = Field(default_factory=list, description="该模块默认关联的用例编号")


class DocRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: Literal["markdown", "openapi"] = "markdown"
    title: str = ""
    version: str = ""
    content: str = ""
    char_count: int = 0
    truncated: bool = False
    missing: bool = False


class ModuleMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    modules: list[ModuleEntry] = Field(default_factory=list)

    def by_path(self, path: str) -> list[ModuleEntry]:
        hits: list[ModuleEntry] = []
        for entry in self.modules:
            for prefix in entry.paths:
                if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                    hits.append(entry)
                    break
        return hits

    def by_api(self, api_path: str) -> list[ModuleEntry]:
        hits: list[ModuleEntry] = []
        for entry in self.modules:
            for prefix in entry.api_paths:
                if api_path == prefix or api_path.startswith(prefix.rstrip("/") + "/"):
                    hits.append(entry)
                    break
        return hits

    def modules_for_keyword(self, text: str) -> list[ModuleEntry]:
        lowered = text.lower()
        hits: list[ModuleEntry] = []
        for entry in self.modules:
            for kw in [entry.module, *entry.keywords]:
                if kw and kw.lower() in lowered:
                    hits.append(entry)
                    break
        return hits


class TestCase(BaseModel):
    """用例目录的一行：可追踪映射，scripts 字段是 runner 唯一认可的测试入口。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
    name: str = Field(min_length=1)
    module: str = ""
    entry: list[str] = Field(
        min_length=1, description="pytest 目标，如 tests/test_orders.py::test_create_order"
    )
    status: Literal["active", "draft", "deprecated", "ui", "manual", "perf"] = "active"
    api: list[str] = Field(default_factory=list, description="覆盖的接口路径")
    notes: str = ""
    last_verified: str = ""

    @model_validator(mode="after")
    def _check_entry(self) -> TestCase:
        for item in self.entry:
            if not item.strip() or any(ch in item for ch in ";&|<>$`\n"):
                raise ValueError(f"用例 {self.id} 的 entry 不是合法的 pytest 目标: {item!r}")
        return self


class CaseCatalog(BaseModel):
    """用例目录：id 唯一；冒烟集必须指向真实存在且可执行的用例。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cases: list[TestCase] = Field(min_length=1)
    smoke: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_catalog(self) -> CaseCatalog:
        ids = [c.id for c in self.cases]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"用例编号重复: {sorted(dupes)}")
        by_id = {c.id: c for c in self.cases}
        for smoke_id in self.smoke:
            case = by_id.get(smoke_id)
            if case is None:
                raise ValueError(f"冒烟集引用了不存在的用例编号: {smoke_id}")
            if case.status != "active":
                raise ValueError(f"冒烟集用例 {smoke_id} 状态为 {case.status}，不能进入必跑集")
        return self

    def get(self, case_id: str) -> TestCase | None:
        return next((c for c in self.cases if c.id == case_id), None)

    def active(self) -> list[TestCase]:
        return [c for c in self.cases if c.status == "active"]


# --------------------------------------------------------------------------- 采集


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    status: str = Field(description="A/M/D/R/C/T/U 之一（git --name-status 首字母）")
    old_path: str | None = None
    added: int = 0
    deleted: int = 0
    binary: bool = False
    excluded: bool = False
    exclusion_reason: str = ""
    modules: list[str] = Field(default_factory=list)
    patch: str = ""


class ContextFile(BaseModel):
    """为 diff 之外的补充上下文：相关源文件全文（截断）或文档片段。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    source: Literal["repo", "doc", "head_snapshot"]
    content: str
    truncated: bool = False
    reason: str = ""


class CollectionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str
    repository: str
    base_sha: str
    head_sha: str
    base_ref: str
    head_ref: str
    merge_base: str
    subject: str = ""
    spec_version: str = "0.1"
    changes: list[FileChange] = Field(default_factory=list)
    context_files: list[ContextFile] = Field(default_factory=list)
    docs: list[DocRef] = Field(default_factory=list)
    modules: list[ModuleEntry] = Field(default_factory=list)
    cases_overview: list[dict] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list, description="本次未分析/被排除的部分")
    warnings: list[str] = Field(default_factory=list)
    max_prompt_chars: int | None = Field(default=None, gt=0)
    diff_total_chars: int = 0
    diff_included_chars: int = 0
    collected_at: datetime = Field(default_factory=utc_now)

    @property
    def analyzed_paths(self) -> list[str]:
        return [c.path for c in self.changes if not c.excluded]


# --------------------------------------------------------------------------- 分析


class ImpactItem(BaseModel):
    """一条影响：必须可定位到文件/接口来源，并标注证据强度。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    change: str = Field(min_length=1, description="改了什么")
    module: str = Field(min_length=1)
    evidence: EvidenceKind
    source_files: list[str] = Field(default_factory=list, description="仓库相对路径")
    source_lines: list[str] = Field(default_factory=list, description="如 src/api/orders.py:12-30")
    business_source: list[str] = Field(default_factory=list, description="引用的业务资料路径或章节")
    existing_cases: list[str] = Field(
        default_factory=list, description="已有用例编号（越权编号会被剔除）"
    )
    note: str = ""


class NewCaseSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    module: str = Field(min_length=1)
    rationale: str = ""
    related_impact: str = ""
    covered_by_existing: list[str] = Field(default_factory=list)


class GapItem(BaseModel):
    """覆盖缺口：明确标为未覆盖，不计入通过数。"""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    module: str = ""
    reason: Literal["no_case", "ui_or_manual", "external", "insufficient_context", "other"] = (
        "no_case"
    )


class ImpactAnalysis(BaseModel):
    """analyze 的唯一输出结构。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = ""
    impacts: list[ImpactItem] = Field(default_factory=list)
    new_case_suggestions: list[NewCaseSuggestion] = Field(default_factory=list)
    gaps: list[GapItem] = Field(default_factory=list)
    recommended_cases: list[str] = Field(
        default_factory=list, description="模型建议回归的已有用例编号"
    )
    uncertain: list[str] = Field(default_factory=list, description="需要人确认的问题")
    analyzed_scope: list[str] = Field(default_factory=list)
    model: str = ""
    model_meta: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- 计划


class PlanEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    name: str
    module: str
    entry: list[str]
    priority: CasePriority
    reason: str = ""

    @classmethod
    def from_case(cls, case: TestCase, priority: CasePriority, reason: str = "") -> PlanEntry:
        return cls(
            case_id=case.id,
            name=case.name,
            module=case.module,
            entry=list(case.entry),
            priority=priority,
            reason=reason,
        )


class RejectedCaseRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    reason: str


class RegressionPlan(BaseModel):
    """plan 的输出：只有这里列出的用例才会被执行。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    project: str
    created_at: datetime = Field(default_factory=utc_now)
    base_sha: str
    head_sha: str
    base_ref: str = ""
    head_ref: str = ""
    state: TaskState = "planned"
    entries: list[PlanEntry] = Field(default_factory=list)
    rejected_case_refs: list[RejectedCaseRef] = Field(default_factory=list)
    uncovered_gaps: list[GapItem] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    new_case_drafts: list[NewCaseSuggestion] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)
    environment_version: str = ""
    environment_confirmed: bool = True
    plan_sha256: str = ""

    def entry(self, case_id: str) -> PlanEntry | None:
        return next((e for e in self.entries if e.case_id == case_id), None)


# --------------------------------------------------------------------------- 运行与报告


class CaseRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    name: str
    entry: list[str]
    priority: CasePriority
    result: CaseResult
    duration_seconds: float = 0.0
    assertions: list[str] = Field(default_factory=list, description="失败断言的简短摘要")
    output_excerpt: str = ""
    executed_at: datetime | None = None


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    project: str
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    state: TaskState = "running"
    plan_sha256: str = ""
    base_sha: str = ""
    head_sha: str = ""
    environment_version: str = ""
    environment_confirmed: bool = True
    command: list[str] = Field(default_factory=list)
    results: list[CaseRunResult] = Field(default_factory=list)
    not_run: list[str] = Field(default_factory=list, description="计划内但未实际执行的用例编号")
    errors: list[str] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)

    def recount(self) -> None:
        totals = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        for item in self.results:
            totals[item.result] = totals.get(item.result, 0) + 1
        totals["executed"] = totals["passed"] + totals["failed"] + totals["error"]
        self.totals = totals


class ReportBundle(BaseModel):
    """Report 的中间数据：报告只从这里取数，不允许凭空补写。"""

    model_config = ConfigDict(extra="forbid")

    plan: RegressionPlan
    record: RunRecord
    context: CollectionContext | None = None
