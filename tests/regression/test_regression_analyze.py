from __future__ import annotations

import json

import pytest

from api_test_agent.regression.analyze import (
    AnalysisError,
    StubAnalyzer,
    _parse_analysis,
    build_analysis_request,
)
from api_test_agent.regression.collect import collect


def _valid_payload() -> dict:
    return {
        "summary": "下单接口增加了库存校验",
        "impacts": [
            {
                "id": "IMP-01",
                "change": "create_order 在 qty>10 时抛错",
                "module": "订单",
                "evidence": "direct",
                "source_files": ["src/orders/api.py"],
                "source_lines": ["src/orders/api.py:2-3"],
                "business_source": ["docs/orders.md"],
                "existing_cases": ["TC-A"],
            }
        ],
        "new_case_suggestions": [
            {"title": "库存不足时返回 400", "module": "订单", "rationale": "边界未覆盖"}
        ],
        "gaps": [{"description": "缺少库存下限校验用例", "module": "订单", "reason": "no_case"}],
        "recommended_cases": ["TC-A", "TC-B"],
        "uncertain": ["库存阈值是否可由配置调整"],
        "analyzed_scope": ["src/orders/api.py"],
    }


def test_parse_accepts_fenced_json():
    raw = "```json\n" + json.dumps(_valid_payload(), ensure_ascii=False) + "\n```"
    analysis = _parse_analysis(raw)
    assert analysis.impacts[0].evidence == "direct"
    assert analysis.recommended_cases == ["TC-A", "TC-B"]


def test_parse_rejects_extra_fields_and_bad_evidence():
    payload = _valid_payload()
    payload["impacts"][0]["evidence"] = "guess"
    with pytest.raises(AnalysisError):
        _parse_analysis(json.dumps(payload, ensure_ascii=False))
    payload = _valid_payload()
    payload["command"] = "pytest -k something"
    with pytest.raises(AnalysisError):
        _parse_analysis(json.dumps(payload, ensure_ascii=False))


def test_parse_rejects_empty_or_non_json():
    with pytest.raises(AnalysisError):
        _parse_analysis("")
    with pytest.raises(AnalysisError):
        _parse_analysis("影响：改了订单接口")


def test_prompt_contains_context_and_diff(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    prompt = build_analysis_request(context, project.prompts_dir)
    assert "src/orders/api.py" in prompt
    assert "库存不足" in prompt
    assert "TC-A" in prompt
    assert "top-secret-value" not in prompt
    assert "{{CONTEXT_JSON}}" not in prompt


def test_stub_analyzer_is_offline_and_marks_uncertain(worlds):
    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    result = StubAnalyzer(project).analyze(context, project.prompts_dir)
    assert result.analysis.model == "stub"
    assert result.analysis.impacts
    assert all(item.evidence == "uncertain" for item in result.analysis.impacts)
    assert "TC-A" in result.analysis.recommended_cases


def test_analyzer_requires_key(worlds, monkeypatch):
    from api_test_agent.regression.analyze import Analyzer

    _, _, project, _, _ = worlds
    monkeypatch.delenv("TESTAGENT_FAKE_KEY", raising=False)
    with pytest.raises(AnalysisError) as info:
        Analyzer(project.config.model, api_key=None)
    assert "TESTAGENT_FAKE_KEY" in str(info.value)


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 20


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]
        self.model = "fake-model"
        self.usage = _FakeUsage()


class _FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _FakeResponse(reply)


def test_analyzer_repairs_invalid_output_once(worlds):
    from api_test_agent.regression.analyze import Analyzer

    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    # 第一轮输出了不允许的字段（例如自行拼出的执行命令），结构校验必然失败
    bad = json.dumps({"summary": "x", "command": "pytest -k order"})
    good = json.dumps(_valid_payload(), ensure_ascii=False)
    analyzer = Analyzer(project.config.model, api_key="fake")
    fake = _FakeClient([bad, good])
    analyzer._client = lambda: fake
    result = analyzer.analyze(context, project.prompts_dir)
    assert result.attempts == 2
    assert len(result.analysis.recommended_cases) == 2
    assert fake.calls[1]["messages"][-1]["content"].startswith("上面的输出不符合约定结构")


def test_analyzer_fails_after_repair(worlds):
    from api_test_agent.regression.analyze import Analyzer

    _, _, project, _, _ = worlds
    context = collect(project, "HEAD~1", "HEAD")
    analyzer = Analyzer(project.config.model, api_key="fake")
    analyzer._client = lambda: _FakeClient(["not json", "still not json"])
    with pytest.raises(AnalysisError):
        analyzer.analyze(context, project.prompts_dir)
