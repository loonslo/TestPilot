"""The public interface description the Agent may read: operations, response notes, rules.

Only files under ``benchmarks/public`` are loaded. The index answers what ``search_spec`` and
``get_operation`` need and renders the compact text that goes into the first prompt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PARAM_RE = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class Operation:
    id: str
    method: str
    path: str
    summary: str
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    description: dict[str, Any]  # what get_operation returns, ready to serialise


def _type_text(schema: dict[str, Any]) -> str:
    if "anyOf" in schema:
        return " or ".join(_type_text(option) for option in schema["anyOf"])
    kind = schema.get("type", "any")
    extras = []
    if "pattern" in schema:
        extras.append(f"pattern {schema['pattern']}")
    if "minLength" in schema or "maxLength" in schema:
        extras.append(f"length {schema.get('minLength', 0)}..{schema.get('maxLength', '')}")
    if "minimum" in schema:
        extras.append(f">= {schema['minimum']}")
    if "enum" in schema:
        extras.append("one of " + "|".join(map(str, schema["enum"])))
    return kind + (f" ({', '.join(extras)})" if extras else "")


def _render(schema: dict[str, Any], components: dict[str, Any], depth: int = 0) -> Any:
    if "$ref" in schema:
        return _render(components[schema["$ref"].rsplit("/", 1)[1]], components, depth)
    if "anyOf" in schema:
        return _type_text(schema)
    if schema.get("type") == "object" and "properties" in schema:
        required = set(schema.get("required", []))
        rendered: dict[str, Any] = {}
        for name, prop in schema["properties"].items():
            if depth >= 2 and "$ref" in prop:
                rendered[name] = "object"
                continue
            inner = _render(prop, components, depth + 1)
            mark = ", required" if name in required else ""
            rendered[name] = (
                f"{inner}{mark}"
                if isinstance(inner, str)
                else {"_": mark.strip(", "), **inner}
                if isinstance(inner, dict)
                else inner
            )
        return rendered
    if schema.get("type") == "array":
        items = schema.get("items", {})
        inner = _render(items, components, depth + 1)
        bounds = f" ({schema.get('minItems', 0)}..{schema.get('maxItems', '')} items)"
        return {"array" + bounds: inner}
    return _type_text(schema)


class Spec:
    def __init__(self, openapi: dict[str, Any], notes: dict[str, Any], rules: dict[str, Any]):
        self._notes = notes
        self._rules = {rule["id"]: rule for rule in rules["rules"]}
        components = openapi.get("components", {}).get("schemas", {})
        self.operations: dict[str, Operation] = {}
        for path, item in openapi["paths"].items():
            for method, op in item.items():
                operation = self._operation(path, method, op, components)
                self.operations[operation.id] = operation

    @classmethod
    def load(cls, public_dir: Path) -> Spec:
        def read(name: str) -> dict[str, Any]:
            return json.loads((public_dir / name).read_text(encoding="utf-8"))

        return cls(read("openapi.json"), read("response-notes.json"), read("rules.json"))

    def _operation(
        self, path: str, method: str, op: dict[str, Any], components: dict[str, Any]
    ) -> Operation:
        op_id = op["operationId"]
        params = op.get("parameters", [])
        path_params = tuple(_PARAM_RE.findall(path))
        query_params = tuple(p["name"] for p in params if p.get("in") == "query")
        body_schema = (
            op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        )
        description: dict[str, Any] = {
            "operation_id": op_id,
            "method": method.upper(),
            "path": path,
            "summary": op.get("summary", ""),
            "path_params": {name: "string, required" for name in path_params},
            "query_params": {
                p["name"]: _type_text(p.get("schema", {}))
                + (", required" if p.get("required") else ", optional")
                for p in params
                if p.get("in") == "query"
            },
            "request_body": _render(body_schema, components) if body_schema else None,
            "response": self._notes["operations"].get(op_id, ""),
        }
        return Operation(
            id=op_id,
            method=method.upper(),
            path=path,
            summary=op.get("summary", ""),
            path_params=path_params,
            query_params=query_params,
            description=description,
        )

    # --- what the tools need ----------------------------------------------------------------

    def search(self, query: str, allowed: Iterable[str], limit: int = 8) -> list[dict[str, str]]:
        words = [w for w in re.split(r"[^a-z0-9_]+", query.lower()) if w]
        scored = []
        for op_id in allowed:
            op = self.operations[op_id]
            haystack = f"{op.id} {op.summary} {op.path} {op.method}".lower()
            score = sum(haystack.count(word) for word in words)
            scored.append((score, op))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        hits = [op for score, op in scored if score > 0] or [op for _, op in scored]
        return [
            {"operation_id": op.id, "method": op.method, "path": op.path, "summary": op.summary}
            for op in hits[:limit]
        ]

    def describe(self, operation_id: str) -> dict[str, Any]:
        return self.operations[operation_id].description

    def rule(self, rule_id: str) -> dict[str, Any]:
        return self._rules[rule_id]

    @property
    def rule_ids(self) -> list[str]:
        return list(self._rules)

    def rules_text(self, rule_ids: Iterable[str]) -> str:
        lines = []
        for rule_id in rule_ids:
            rule = self._rules[rule_id]
            lines.append(f"- {rule_id} {rule['title']}: {rule['statement']}")
        return "\n".join(lines)

    def conventions_text(self) -> str:
        lines = [f"- {name}: {text}" for name, text in self._notes["conventions"].items()]
        lines.append("Response objects (fields of `data` entries):")
        for name, fields in self._notes["objects"].items():
            parts = ", ".join(f"{field} ({kind})" for field, kind in fields.items())
            lines.append(f"- {name}: {parts}")
        return "\n".join(lines)
