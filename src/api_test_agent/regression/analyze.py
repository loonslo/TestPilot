"""analyze 模块：调用一个模型接口，把采集上下文变成结构化影响分析。

边界：
- 只发一个请求（失败时按可重试错误重试），不做多轮自由规划。
- 模型只能"建议"已有用例编号，编号真实性由 plan 校验；模型不生成任何可直接执行的命令。
- 输出必须是 JSON 对象，校验失败时把错误摘要回传模型修正一次；仍失败则明确报错，不猜。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from api_test_agent.regression.models import (
    CollectionContext,
    ImpactAnalysis,
    ImpactItem,
    ModelConfig,
)
from api_test_agent.regression.project import format_validation

logger = logging.getLogger(__name__)


class AnalysisError(RuntimeError):
    """调用模型或校验其输出失败。"""


@dataclass(frozen=True)
class AnalysisResult:
    analysis: ImpactAnalysis
    prompt_chars: int
    response_chars: int
    latency_seconds: float
    attempts: int
    prompt_path: Path | None = None
    raw_response: str = ""


def build_prompt(context: CollectionContext, prompts_dir: Path) -> str:
    return build_analysis_request(context, prompts_dir)


def _load_template(prompts_dir: Path) -> str:
    template_path = prompts_dir / "analyze.md"
    if not template_path.is_file():
        raise AnalysisError(f"缺少提示词模板: {template_path}")
    return template_path.read_text(encoding="utf-8")


def _context_payload(context: CollectionContext) -> dict[str, Any]:
    return {
        "project": context.project,
        "versions": {
            "base_ref": context.base_ref,
            "base_sha": context.base_sha,
            "head_ref": context.head_ref,
            "head_sha": context.head_sha,
            "merge_base": context.merge_base,
            "subjects": context.subject,
        },
        "budget": {
            "diff_total_chars": context.diff_total_chars,
            "diff_included_chars": context.diff_included_chars,
        },
        "files": [
            {
                "path": c.path,
                "status": c.status,
                "old_path": c.old_path,
                "added": c.added,
                "deleted": c.deleted,
                "modules": c.modules,
                "binary": c.binary,
                "excluded": c.excluded,
                "exclusion_reason": c.exclusion_reason,
            }
            for c in context.changes
        ],
        "modules": [
            {
                "module": m.module,
                "summary": m.summary,
                "paths": m.paths,
                "api_paths": m.api_paths,
                "keywords": m.keywords,
                "default_cases": m.cases,
            }
            for m in context.modules
        ],
        "case_catalog": context.cases_overview,
        "context_files": [
            {
                "path": f.path,
                "source": f.source,
                "content": f.content,
                "truncated": f.truncated,
                "reason": f.reason,
            }
            for f in context.context_files
        ],
        "business_docs": [
            {
                "path": d.path,
                "title": d.title,
                "version": d.version,
                "kind": d.kind,
                "content": d.content,
            }
            for d in context.docs
            if not d.missing
        ],
        "omissions": context.omissions,
        "warnings": context.warnings,
    }


def _diff_section(context: CollectionContext) -> str:
    parts: list[str] = ["# 变更内容（unified diff，含路径）", ""]
    for change in context.changes:
        if change.patch:
            parts.append(f"## {change.status} {change.path}")
            parts.append("```diff")
            parts.append(change.patch.rstrip())
            parts.append("```")
            parts.append("")
    return "\n".join(parts)


def build_analysis_request(context: CollectionContext, prompts_dir: Path) -> str:
    template = _load_template(prompts_dir)
    if "{{CONTEXT_JSON}}" not in template:
        raise AnalysisError(f"{prompts_dir / 'analyze.md'} 缺少 {{{{CONTEXT_JSON}}}} 占位符")

    def render() -> str:
        text = template.replace(
            "{{CONTEXT_JSON}}",
            json.dumps(_context_payload(context), ensure_ascii=False, indent=1),
        )
        return text + "\n\n" + _diff_section(context)

    prompt = render()
    if context.max_prompt_chars is None:
        return prompt
    candidates = [
        (context.context_files, i, "content") for i in reversed(range(len(context.context_files)))
    ]
    candidates += [(context.docs, i, "content") for i in reversed(range(len(context.docs)))]
    candidates += [(context.changes, i, "patch") for i in reversed(range(len(context.changes)))]
    for group, index, field in candidates:
        if len(prompt) <= context.max_prompt_chars:
            break
        item = group[index]
        path = item.path
        if not getattr(item, field):
            continue
        updates = {field: ""}
        if hasattr(item, "truncated"):
            updates["truncated"] = True
        if field == "patch":
            updates["excluded"] = True
            updates["exclusion_reason"] = "Final serialized prompt budget"
        group[index] = item.model_copy(update=updates)
        context.omissions.append(f"{path}: content omitted by final serialized prompt budget")
        context.diff_included_chars = sum(len(c.patch) for c in context.changes)
        prompt = render()
    if len(prompt) > context.max_prompt_chars:
        raise AnalysisError("Input budget cannot hold template and provenance metadata")
    return prompt


SYSTEM_PROMPT = (
    "你是接口回归分析助手。只根据给定材料判断变更影响与回归范围。"
    "不编造文件、接口、用例编号或业务规则；不确定的结论必须写进 uncertain 或 gaps。"
    "只输出一个 JSON 对象，不输出解释文字。"
)


class Analyzer:
    """封装"一次模型调用 + 结构校验"，可被替换为离线桩用于自测。"""

    def __init__(self, config: ModelConfig | None, *, api_key: str | None = None) -> None:
        if config is not None and not api_key:
            raise AnalysisError(
                f"缺少模型密钥：请设置环境变量 {config.api_key_env}（密钥不写入配置或模型上下文）"
            )
        self.config = config
        self.api_key = api_key

    def _client(self):
        from openai import OpenAI

        assert self.config is not None
        return OpenAI(
            base_url=self.config.base_url,
            api_key=self.api_key,
            timeout=self.config.timeout_seconds,
            max_retries=0,
        )

    def _chat(self, client, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        assert self.config is not None
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        if self.config.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
        message = response.choices[0].message if response.choices else None
        content = (message.content if message else "") or ""
        usage = getattr(response, "usage", None)
        meta = {
            "model": getattr(response, "model", self.config.model),
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
        }
        return content, meta

    def analyze(
        self,
        context: CollectionContext,
        prompts_dir: Path,
        *,
        on_prompt: Callable[[str], Path | None] | None = None,
    ) -> AnalysisResult:
        if self.config is None:
            raise AnalysisError("项目配置没有 [model] 段；离线自测请使用 --stub 或注入分析桩")
        prompt = build_analysis_request(context, prompts_dir)
        prompt_path = on_prompt(prompt) if on_prompt else None
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        client = self._client()
        started = time.monotonic()
        attempts = 0
        last_error = ""
        raw = ""
        meta: dict[str, Any] = {}
        while attempts < self.config.max_attempts:
            attempts += 1
            backoff = min(2 ** (attempts - 1), 8)
            try:
                raw, meta = self._chat(client, messages)
            except Exception as exc:  # noqa: BLE001 - 分类后继续或退出
                last_error = _classify(exc)
                if attempts < self.config.max_attempts:
                    logger.warning(
                        "模型调用失败（第 %d 次）：%s，%ss 后重试", attempts, last_error, backoff
                    )
                    time.sleep(backoff)
                    continue
                raise AnalysisError(f"模型调用失败: {last_error}") from exc
            try:
                analysis = _parse_analysis(raw)
            except AnalysisError as exc:
                last_error = str(exc)
                if attempts < self.config.max_attempts:
                    logger.warning("模型输出结构不合法，回传错误并要求修正一次")
                    messages = messages + [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": (
                                "上面的输出不符合约定结构，错误如下：\n"
                                f"{last_error}\n只重新输出修正后的 JSON 对象，不要输出其他文字。"
                            ),
                        },
                    ]
                    continue
                raise
            analysis.model = str(meta.get("model") or self.config.model)
            analysis.model_meta = meta
            return AnalysisResult(
                analysis=analysis,
                prompt_chars=len(prompt),
                response_chars=len(raw),
                latency_seconds=time.monotonic() - started,
                attempts=attempts,
                prompt_path=prompt_path,
                raw_response=raw,
            )
        raise AnalysisError(f"模型调用失败: {last_error or '未知原因'}")


_FATAL_STATUS = (400, 401, 403, 404, 422)


def _classify(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status in _FATAL_STATUS:
        return f"{name} HTTP {status}（不可重试，请检查 base_url/model/api_key）"
    if status is not None:
        return f"{name} HTTP {status}"
    return f"{name}: {str(exc)[:300]}"


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _parse_analysis(raw: str) -> ImpactAnalysis:
    text = _strip_fence(raw)
    if not text:
        raise AnalysisError("模型返回为空")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise AnalysisError("模型输出不含 JSON 对象")
    payload = text[start : end + 1]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"模型输出不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AnalysisError("模型输出的 JSON 顶层必须是对象")
    try:
        return ImpactAnalysis.model_validate(data)
    except ValidationError as exc:
        raise AnalysisError(f"模型输出结构校验失败:\n{format_validation(exc)}") from exc


class StubAnalyzer:
    """离线桩：不调用模型，按模块映射与用例目录生成"保守但真实"的建议。

    用途：在没有模型密钥时验证 collect/plan/runner/report 全链路，以及 CI 回归。
    它不会假装读过 diff 的语义，所有条目都标为 uncertain/inferred，并在报告中注明"离线桩"。
    """

    def __init__(self, project: Any) -> None:
        self.module_map = project.module_map
        self.catalog = project.catalog

    def analyze(
        self, context: CollectionContext, prompts_dir: Path, *, on_prompt=None
    ) -> AnalysisResult:
        prompt = build_analysis_request(context, prompts_dir)
        prompt_path = on_prompt(prompt) if on_prompt else None
        impacts = []
        recommended: list[str] = []
        for index, change in enumerate(context.changes, start=1):
            if change.excluded:
                continue
            modules = change.modules or [e.module for e in self.module_map.by_path(change.path)]
            cases = []
            for module in modules:
                entry = next((m for m in self.module_map.modules if m.module == module), None)
                if entry:
                    cases.extend(c for c in entry.cases if self.catalog.get(c))
                for case in self.catalog.cases:
                    if case.module == module and case.id not in cases:
                        cases.append(case.id)
            impacts.append(
                {
                    "id": f"IMP-{index:02d}",
                    "change": f"{change.path} 有代码变更（{change.added}+/{change.deleted}-）",
                    "module": modules[0] if modules else "未映射",
                    "evidence": "uncertain",
                    "source_files": [change.path],
                    "business_source": [],
                    "existing_cases": cases,
                    "note": "离线桩按模块映射生成，未做语义分析",
                }
            )
            recommended.extend(cases)
        analysis = ImpactAnalysis(
            summary="离线桩输出：只依据模块映射与用例目录给出候选回归范围，未做语义分析。",
            impacts=[ImpactItem.model_validate(i) for i in impacts],
            recommended_cases=sorted(set(recommended)),
            gaps=[],
            uncertain=["离线桩不判断业务影响，请在使用 --stub 时把结果只当作链路验证"],
            analyzed_scope=context.analyzed_paths,
            model="stub",
            model_meta={"model": "stub"},
        )
        return AnalysisResult(
            analysis=analysis,
            prompt_chars=len(prompt),
            response_chars=len(analysis.model_dump_json()),
            latency_seconds=0.0,
            attempts=0,
            prompt_path=prompt_path,
            raw_response="",
        )
