"""OpenAPI and Swagger specification discovery adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from api_test_agent.tasks.models import SourceRef

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader: StrictLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, (str, int)):
            raise ValueError("document mapping keys must be strings or integer status codes")
        key = str(key)
        if key in result:
            raise ValueError("duplicate document mapping key; reconcile declarations explicitly")
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def resolve_refs(value: Any, root: dict, seen: frozenset[str] = frozenset(), depth: int = 0) -> Any:
    if depth > 32:
        return {"$ref": "unresolved-depth-limit"}
    if isinstance(value, list):
        return [resolve_refs(item, root, seen, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str):
        if not reference.startswith("#/") or reference in seen:
            return value
        target: Any = root
        try:
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except KeyError, TypeError:
            return value
        merged = resolve_refs(target, root, seen | {reference}, depth + 1)
        if not isinstance(merged, dict):
            return value
        return merged | {
            k: resolve_refs(v, root, seen, depth + 1) for k, v in value.items() if k != "$ref"
        }
    return {k: resolve_refs(v, root, seen, depth + 1) for k, v in value.items()}


def openapi(content: str, source: SourceRef) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse an OpenAPI (v3) or Swagger (v2) document into discovery candidate rows."""
    data = yaml.load(content, Loader=StrictLoader)
    if not isinstance(data, dict) or not ("openapi" in data or "swagger" in data):
        return [], [f"{source.path}: no OpenAPI document; retained as rule material"]
    warnings: list[str] = []
    rows = []
    prefix = data.get("basePath", "")
    if not isinstance(prefix, str):
        raise ValueError("OpenAPI basePath must be text")
    servers = data.get("servers", [])
    if not isinstance(servers, list) or any(not isinstance(server, dict) for server in servers):
        raise ValueError("OpenAPI servers must be a list of objects")
    ambiguous_server = False
    if servers:
        prefix = urlsplit(str(servers[0].get("url", ""))).path.rstrip("/")
        if len(servers) > 1 or "{" in prefix:
            ambiguous_server = True
            warnings.append(f"{source.path}: multiple/variable servers need confirmation")
    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI paths must be a mapping")
    for path, item in paths.items():
        item = resolve_refs(item, data)
        if not isinstance(item, dict):
            warnings.append(f"{source.path}: malformed path {path}")
            continue
        if "$ref" in item:
            warnings.append(f"{source.path}: unresolved path reference {path}")
        for method, detail in item.items():
            if method not in METHODS:
                continue
            if not isinstance(detail, dict):
                warnings.append(f"{source.path}: malformed operation {method} {path}")
                continue
            detail = dict(detail)
            if (
                not isinstance(item.get("parameters", []), list)
                or not isinstance(detail.get("parameters", []), list)
                or not isinstance(detail.get("responses", {}), dict)
            ):
                raise ValueError("OpenAPI parameters/responses have invalid shapes")
            detail["parameters"] = [*item.get("parameters", []), *detail.get("parameters", [])]
            full_path = prefix.rstrip("/") + "/" + str(path).lstrip("/")
            unsupported = any(k in json.dumps(detail) for k in ("multipart/", "text/event-stream"))
            rows.append(
                dict(
                    method=method.upper(),
                    path=full_path,
                    original_operation_ids=[str(detail.get("operationId", ""))],
                    mount_prefix=prefix,
                    sources=[source],
                    status=(
                        "unsupported"
                        if unsupported
                        else "unresolved"
                        if "$ref" in json.dumps(detail) or ambiguous_server
                        else "confirmed"
                    ),
                    reason="Document declaration; runtime not checked",
                    details={
                        "kind": "requirement",
                        "parameters": detail.get("parameters", []),
                        "authentication": detail.get("security", data.get("security", [])),
                        "responses": detail.get("responses", {}),
                        "request_body": detail.get("requestBody"),
                        "locator": f"paths.{path}.{method}",
                        "raw": detail,
                    },
                    signature=json.dumps(detail, sort_keys=True),
                )
            )
    return rows, warnings


class OpenAPIAdapter:
    """Adapter for OpenAPI 3.x and Swagger 2.0 specifications."""

    name = "openapi"
    supported_extensions = (".json", ".yaml", ".yml")

    def extract_document(
        self, content: str, source: SourceRef
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return openapi(content, source)

    def extract(
        self, snapshots: dict[str, str], sources: dict[str, SourceRef]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        all_rows: list[dict[str, Any]] = []
        all_warnings: list[str] = []
        for path, content in sorted(snapshots.items()):
            ext = Path(path).suffix.lower()
            if ext not in self.supported_extensions:
                continue
            source = sources.get(path)
            if not source:
                continue
            rows, warnings = openapi(content, source)
            all_rows.extend(rows)
            all_warnings.extend(warnings)
        return all_rows, all_warnings
