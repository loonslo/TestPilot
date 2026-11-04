"""OpenAPI and Swagger specification discovery adapter."""

from __future__ import annotations

import json
import posixpath
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

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


def _walk_external_refs(value: Any, _seen: frozenset[str] = frozenset()) -> list[str]:
    """Collect $ref strings that point outside the current document.

    A $ref is external when it is not a fragment-only reference (``#/...``).
    In-document fragments that could not be resolved are reported separately,
    so only genuinely external/cross-file references are flagged here.
    """
    refs: list[str] = []
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if not ref.startswith("#/") and not ref.startswith("#"):
                    if ref not in _seen:
                        refs.append(ref)
                        _seen = _seen | {ref}
                continue
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return refs


def _merge_parameters(path_params: list[Any], operation_params: list[Any]) -> list[dict[str, Any]]:
    """Merge path- and operation-level parameters, operation level winning by name.

    Per OpenAPI, an operation-level parameter with the same (name, in) pair
    overrides its path-level counterpart. Path-level declarations that are not
    overridden are retained in document order.
    """
    merged: list[dict[str, Any]] = []
    index: dict[tuple[str, str], int] = {}
    for params in (path_params, operation_params):
        for param in params:
            if not isinstance(param, dict) or "in" not in param or "name" not in param:
                merged.append(param)
                continue
            key = (str(param.get("in", "")), str(param.get("name", "")))
            if key in index:
                # Operation-level (the second pass) overrides the path-level entry.
                merged[index[key]] = param
            else:
                merged.append(param)
                index[key] = len(merged) - 1
    return merged


def openapi(
    content: str,
    source: SourceRef,
    snapshots: dict[str, str] | None = None,
    sources: dict[str, SourceRef] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse an OpenAPI (v3) or Swagger (v2) document into discovery candidate rows."""
    data = yaml.load(content, Loader=StrictLoader)
    if not isinstance(data, dict) or not ("openapi" in data or "swagger" in data):
        return [], [f"{source.path}: no OpenAPI document; retained as rule material"]
    warnings: list[str] = []
    rows = []
    documents = {source.path: data}
    used: set[str] = set()

    def resolve(value: Any, current: str, seen: frozenset = frozenset(), depth: int = 0) -> Any:
        if depth > 32:
            return {"$ref": "unresolved-depth-limit"}
        if isinstance(value, list):
            return [resolve(v, current, seen, depth + 1) for v in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str):
            url = urlsplit(reference)
            current_source = (sources or {}).get(current, source)
            if current_source.source_url and not reference.startswith("#"):
                from api_test_agent.tasks.openapi_import import snapshot_path

                absolute, _ = urldefrag(urljoin(current_source.source_url, reference))
                target_path = snapshot_path(absolute)
            else:
                if url.scheme or url.netloc or url.query or "\\" in url.path:
                    return value
                target_path = (
                    posixpath.normpath(
                        posixpath.join(posixpath.dirname(current), unquote(url.path))
                    )
                    if url.path
                    else current
                )
            marker = (target_path, url.fragment)
            if marker in seen or target_path.startswith(("../", "/")):
                return value
            if target_path not in documents:
                if not snapshots or not sources or target_path not in sources:
                    return value
                if target_path not in snapshots:
                    return value
                documents[target_path] = yaml.load(snapshots[target_path], Loader=StrictLoader)
            target = documents[target_path]
            fragment = unquote(url.fragment)
            if fragment and not fragment.startswith("/"):
                return value
            try:
                for part in fragment[1:].split("/") if fragment else []:
                    key = part.replace("~1", "/").replace("~0", "~")
                    target = target[int(key)] if isinstance(target, list) else target[key]
            except KeyError, TypeError, ValueError, IndexError:
                return value
            used.add(target_path)
            resolved = resolve(target, target_path, seen | {marker}, depth + 1)
            if not isinstance(resolved, dict):
                return value
            return resolved | {
                k: resolve(v, current, seen, depth + 1) for k, v in value.items() if k != "$ref"
            }
        return {k: resolve(v, current, seen, depth + 1) for k, v in value.items()}

    prefix = data.get("basePath", "")
    if not isinstance(prefix, str):
        raise ValueError("OpenAPI basePath must be text")
    servers = data.get("servers", [])
    if not isinstance(servers, list) or any(not isinstance(server, dict) for server in servers):
        raise ValueError("OpenAPI servers must be a list of objects")
    ambiguous_server = False

    def _prefix_for(server_list: Any) -> tuple[str, bool]:
        """Resolve a server list to a mount prefix and whether it is ambiguous.

        The most specific server declaration wins per OpenAPI: the operation
        level overrides the path level, which overrides the document root.
        """
        if not isinstance(server_list, list):
            raise ValueError("OpenAPI servers must be a list of objects")
        if not server_list:
            return "", False
        if any(not isinstance(server, dict) for server in server_list):
            raise ValueError("OpenAPI servers must be a list of objects")
        first = urlsplit(str(server_list[0].get("url", ""))).path.rstrip("/")
        ambig = len(server_list) > 1 or "{" in first
        return first, ambig

    root_prefix, root_ambig = _prefix_for(servers)
    if root_ambig:
        warnings.append(f"{source.path}: multiple/variable servers need confirmation")
    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI paths must be a mapping")
    for path, item in paths.items():
        used = {source.path}
        item = resolve(item, source.path)
        if not isinstance(item, dict):
            warnings.append(f"{source.path}: malformed path {path}")
            continue
        if "$ref" in item:
            warnings.append(f"{source.path}: unresolved path reference {path}")
        path_prefix, path_ambig = (
            _prefix_for(item["servers"]) if "servers" in item else (root_prefix, root_ambig)
        )
        # A server override replaces the root URL entirely; otherwise fall back to
        # basePath (Swagger v2) when no v3 servers carry a path.
        if "servers" not in item and "servers" not in data:
            path_prefix = data.get("basePath", "")
        if "$ref" in json.dumps(item):
            for ref in _walk_external_refs(item):
                warnings.append(f"{source.path}: external/cross-file $ref {ref} not resolved")
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
            prefix, ambiguous_server = (
                _prefix_for(detail["servers"]) if "servers" in detail else (path_prefix, path_ambig)
            )
            detail["parameters"] = _merge_parameters(
                item.get("parameters", []), detail.get("parameters", [])
            )
            full_path = prefix.rstrip("/") + "/" + str(path).lstrip("/")
            unsupported = any(k in json.dumps(detail) for k in ("multipart/", "text/event-stream"))
            if "$ref" in json.dumps(detail):
                for ref in _walk_external_refs(detail):
                    warnings.append(f"{source.path}: external/cross-file $ref {ref} not resolved")
            rows.append(
                dict(
                    method=method.upper(),
                    path=full_path,
                    original_operation_ids=[str(detail.get("operationId", ""))],
                    mount_prefix=prefix,
                    sources=[(sources or {}).get(p, source) for p in sorted(used)],
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
            rows, warnings = openapi(content, source, snapshots, sources)
            all_rows.extend(rows)
            all_warnings.extend(warnings)
        return all_rows, all_warnings
