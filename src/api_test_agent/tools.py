"""The four tools of the Agent and everything that guards them.

``search_spec`` and ``get_operation`` read the public spec. ``http_call`` sends one request to the
sandbox target through ``TargetHttp``. ``submit_case`` stores a validated case. Every tool answers
with JSON text, including refusals and errors, so the conversation protocol always closes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api_test_agent.budget import Budget
from api_test_agent.cases import CaseModel, validate_case
from api_test_agent.spec import Spec
from api_test_agent.targethttp import CallError, TargetHttp, bound_for_model

MAX_CASES = 5
TOOL_NAMES = ("search_spec", "get_operation", "http_call", "submit_case")


@dataclass(frozen=True)
class TaskScope:
    """What one task allows: rules to judge by, operations and callers to use, limits."""

    task_id: str
    title: str
    brief: str
    rules: tuple[str, ...]
    operations: tuple[str, ...]
    callers: tuple[str, ...]
    limits: dict[str, float]

    @classmethod
    def load(cls, path: Path) -> TaskScope:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            task_id=raw["task_id"],
            title=raw["title"],
            brief=raw["brief"],
            rules=tuple(raw["rules"]),
            operations=tuple(raw["operations"]),
            callers=tuple(raw["callers"]),
            limits=dict(raw["limits"]),
        )


@dataclass(frozen=True)
class ToolOutcome:
    tool: str
    content: str  # the JSON text returned to the model
    status: Literal["ok", "error", "refused"]
    signature: str  # the same call twice has the same signature


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


class _SearchArgs(_Args):
    query: str = Field(min_length=1, max_length=200)


class _DescribeArgs(_Args):
    operation_id: str


class _HttpArgs(_Args):
    operation_id: str
    caller: str
    path_params: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None


def _problems(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors()[:5]
    )


def tool_schemas(scope: TaskScope) -> list[dict[str, Any]]:
    """The function definitions sent to the model. Plain types only: providers differ in what
    JSON Schema they accept, so maps are free-form objects and the host validates the rest."""
    operation = {"type": "string", "enum": list(scope.operations)}
    caller = {"type": "string", "enum": list(scope.callers)}
    string_map = {"type": "object"}
    return [
        {
            "type": "function",
            "function": {
                "name": "search_spec",
                "description": "Find operations by keywords (name, path or summary). Only operations allowed in this task are listed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "keywords such as 'archive transaction'",
                        }
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_operation",
                "description": "Full details of one operation: method, path, parameters, request body fields and what the answer looks like.",
                "parameters": {
                    "type": "object",
                    "properties": {"operation_id": operation},
                    "required": ["operation_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "http_call",
                "description": "Call one operation as one caller. Costs one unit of the HTTP budget. Path placeholders go in path_params, query parameters in query (all values strings), the JSON request body in body.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation_id": operation,
                        "caller": caller,
                        "path_params": {
                            **string_map,
                            "description": "values for {placeholders} in the path",
                        },
                        "query": {**string_map, "description": "query parameters"},
                        "body": {"type": "object", "description": "JSON request body"},
                    },
                    "required": ["operation_id", "caller"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_case",
                "description": (
                    "Submit a replayable test case for a behaviour that contradicts a rule. The case must state what the RULE requires, so it passes on a correct service and fails on a faulty one, and it must create its own data. "
                    "Steps run in order on a fresh service. Later steps and checks refer to earlier answers as {{step_id.path}}, for example {{s1.data.account_id}}, {{s2.data.items.0.version}} or {{s3.status}}; a string that is exactly one placeholder keeps the value's type. "
                    'Put expectations on a step (expect_status, expect_error_code) or in checks. A check is one comparison written with + - * == != < <= > >=, len(x), abs(x) and sum(list, "field"), for example {{s4.data.base.income_minor}} == 0.'
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string", "description": "lowercase id such as c1"},
                        "title": {"type": "string"},
                        "rule_id": {"type": "string", "enum": list(scope.rules)},
                        "claim": {
                            "type": "string",
                            "description": "the behaviour the rule requires, in one sentence",
                        },
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "description": "lowercase id such as s1",
                                    },
                                    "operation_id": operation,
                                    "caller": caller,
                                    "path_params": string_map,
                                    "query": string_map,
                                    "body": {"type": "object"},
                                    "expect_status": {"type": "integer"},
                                    "expect_error_code": {"type": "string"},
                                },
                                "required": ["id", "operation_id", "caller"],
                            },
                        },
                        "checks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "expr": {"type": "string"},
                                    "message": {"type": "string"},
                                },
                                "required": ["expr"],
                            },
                        },
                    },
                    "required": ["case_id", "title", "rule_id", "claim", "steps"],
                },
            },
        },
    ]


class ToolBox:
    def __init__(
        self,
        scope: TaskScope,
        spec: Spec,
        http: TargetHttp,
        budget: Budget,
        run_dir: Path,
    ) -> None:
        self.scope = scope
        self._spec = spec
        self._http = http
        self._budget = budget
        self._evidence_dir = run_dir / "evidence"
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        self._cases_file = run_dir / "cases.json"
        self.cases: list[CaseModel] = []
        self.calls_made = 0
        self._evidence_count = 0

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return tool_schemas(self.scope)

    # --- dispatch --------------------------------------------------------------------------------

    def dispatch(self, name: str, raw_arguments: str) -> ToolOutcome:
        signature = f"{name}:{raw_arguments.strip()}"
        handler = {
            "search_spec": self._search,
            "get_operation": self._describe,
            "http_call": self._http_call,
            "submit_case": self._submit,
        }.get(name)
        if handler is None:
            return self._answer(
                name,
                signature,
                "error",
                {"error": f"unknown tool {name!r}; use one of {list(TOOL_NAMES)}"},
            )
        self.calls_made += 1
        try:
            arguments = json.loads(raw_arguments or "{}")
        except ValueError as exc:
            return self._answer(
                name, signature, "error", {"error": f"the arguments are not valid JSON: {exc}"}
            )
        if not isinstance(arguments, dict):
            return self._answer(
                name, signature, "error", {"error": "the arguments must be a JSON object"}
            )
        return handler(name, signature, arguments)

    @staticmethod
    def _answer(
        tool: str,
        signature: str,
        status: Literal["ok", "error", "refused"],
        payload: dict[str, Any],
    ) -> ToolOutcome:
        return ToolOutcome(tool, json.dumps(payload, ensure_ascii=False), status, signature)

    # --- the tools ---------------------------------------------------------------------------------

    def _search(self, tool: str, signature: str, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            args = _SearchArgs.model_validate(arguments)
        except ValidationError as exc:
            return self._answer(tool, signature, "error", {"error": _problems(exc)})
        hits = self._spec.search(args.query, self.scope.operations)
        return self._answer(tool, signature, "ok", {"operations": hits})

    def _describe(self, tool: str, signature: str, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            args = _DescribeArgs.model_validate(arguments)
        except ValidationError as exc:
            return self._answer(tool, signature, "error", {"error": _problems(exc)})
        if args.operation_id not in self.scope.operations:
            return self._answer(
                tool,
                signature,
                "error",
                {
                    "error": f"{args.operation_id!r} is not an operation of this task",
                    "allowed": list(self.scope.operations),
                },
            )
        return self._answer(tool, signature, "ok", self._spec.describe(args.operation_id))

    def _http_call(self, tool: str, signature: str, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            args = _HttpArgs.model_validate(arguments)
        except ValidationError as exc:
            return self._answer(tool, signature, "error", {"error": _problems(exc)})
        if args.operation_id not in self.scope.operations:
            return self._answer(
                tool,
                signature,
                "error",
                {
                    "error": f"{args.operation_id!r} is not an operation of this task",
                    "allowed": list(self.scope.operations),
                },
            )
        if args.caller not in self.scope.callers:
            return self._answer(
                tool,
                signature,
                "error",
                {
                    "error": f"caller {args.caller!r} is not allowed here",
                    "allowed": list(self.scope.callers),
                },
            )
        refusal = self._budget.http_call_refusal()
        if refusal:
            return self._answer(
                tool,
                signature,
                "refused",
                {
                    "refused": refusal,
                    "message": "the HTTP budget is used up; write your final report",
                },
            )
        try:
            request = self._http.prepare(
                args.operation_id, args.caller, args.path_params, args.query, args.body
            )
        except CallError as exc:
            return self._answer(tool, signature, "error", {"error": str(exc)})
        self._budget.count_http_call()
        try:
            result = self._http.send(request)
        except httpx.HTTPError as exc:
            return self._answer(
                tool,
                signature,
                "error",
                {
                    "error": f"the request failed: {type(exc).__name__}",
                    "http_calls_left": self._http_left(),
                },
            )
        self._evidence_count += 1
        evidence_id = f"e{self._evidence_count}"
        (self._evidence_dir / f"{evidence_id}.json").write_text(
            json.dumps(
                {
                    "operation_id": args.operation_id,
                    "caller": args.caller,
                    "path_params": args.path_params,
                    "query": args.query,
                    "request_body": args.body,
                    "status": result.status,
                    "error_code": result.error_code,
                    "response": result.body if result.body is not None else result.raw_text,
                    "seconds": round(result.seconds, 3),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        shown, truncated = bound_for_model(result)
        return self._answer(
            tool,
            signature,
            "ok",
            {
                "status": result.status,
                "error_code": result.error_code,
                "body": shown,
                "truncated": truncated,
                "evidence_id": evidence_id,
                "http_calls_left": self._http_left(),
            },
        )

    def _http_left(self) -> int:
        return max(0, self._budget.limits.max_http_calls - self._budget.http_calls)

    def _submit(self, tool: str, signature: str, arguments: dict[str, Any]) -> ToolOutcome:
        if len(self.cases) >= MAX_CASES:
            return self._answer(
                tool,
                signature,
                "refused",
                {"refused": "max_cases", "message": f"at most {MAX_CASES} cases per task"},
            )
        try:
            case = CaseModel.model_validate(arguments)
        except ValidationError as exc:
            return self._answer(
                tool, signature, "error", {"accepted": False, "problems": _problems(exc)}
            )
        problems = validate_case(
            case,
            self._spec,
            operations=self.scope.operations,
            callers=self.scope.callers,
            rules=self.scope.rules,
        )
        if any(existing.case_id == case.case_id for existing in self.cases):
            problems.append(f"case_id {case.case_id!r} was already submitted")
        if problems:
            return self._answer(tool, signature, "error", {"accepted": False, "problems": problems})
        self.cases.append(case)
        self._cases_file.write_text(
            json.dumps([c.model_dump() for c in self.cases], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self._answer(
            tool,
            signature,
            "ok",
            {"accepted": True, "case_id": case.case_id, "cases_submitted": len(self.cases)},
        )
