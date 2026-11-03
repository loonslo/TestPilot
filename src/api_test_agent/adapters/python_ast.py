"""Resolve the supported FastAPI routing graph without importing user modules.

Unsupported control flow and expressions are recorded, never evaluated.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from api_test_agent.tasks.models import SourceRef

HTTP = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


@dataclass
class Router:
    key: str
    kind: str
    prefix: str
    source: SourceRef
    auth: list[str] = field(default_factory=list)


@dataclass
class Route:
    owner: str
    path: str
    methods: list[str]
    source: SourceRef
    line: int
    details: dict[str, Any]
    unsupported: bool = False


def literal(node: ast.AST | None, default: Any = None) -> Any:
    if node is None:
        return default
    try:
        return ast.literal_eval(node)
    except ValueError, TypeError, SyntaxError, MemoryError, RecursionError:
        raise ValueError("dynamic expression") from None


def keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def join_path(*parts: str) -> str:
    # Keep trailing slash on the leaf: FastAPI treats it as a distinct declaration.
    combined = "/".join(p.strip("/") for p in parts if p.strip("/"))
    return "/" + combined + ("/" if parts and parts[-1].endswith("/") and combined else "")


def extract(
    snapshots: dict[str, str], sources: dict[str, SourceRef]
) -> tuple[list[dict], list[str]]:
    trees: dict[str, ast.Module] = {}
    modules: dict[str, str] = {}
    notes: list[str] = []
    for path, content in snapshots.items():
        if not path.endswith(".py"):
            continue
        try:
            trees[path] = ast.parse(content)
            modules[path] = module_name(path)
        except SyntaxError:
            notes.append(f"{path}: Python syntax could not be parsed")
    aliases: dict[str, dict[str, str]] = {}
    for path, tree in trees.items():
        mapping: dict[str, str] = {}
        package = modules[path].split(".")
        if not path.endswith("/__init__.py"):
            package = package[:-1]
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    prefix = package[: len(package) - node.level + 1]
                    base = ".".join([*prefix, *([base] if base else [])])
                for name_node in node.names:
                    mapping[name_node.asname or name_node.name] = f"{base}.{name_node.name}".strip(
                        "."
                    )
            elif isinstance(node, ast.Import):
                for name_node in node.names:
                    mapping[name_node.asname or name_node.name.split(".")[0]] = (
                        name_node.name if name_node.asname else name_node.name.split(".")[0]
                    )
        aliases[path] = mapping

    def name(path: str, expression: ast.AST) -> str:
        parts = ast.unparse(expression).split(".")
        mapped = aliases[path].get(parts[0])
        return ".".join([mapped, *parts[1:]]) if mapped else f"{modules[path]}." + ".".join(parts)

    routers: dict[str, Router] = {}
    calls: list[tuple[str, ast.Call, ast.AST | None]] = []
    for path, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                call = node.value
                factory = name(path, call.func)
                if factory in {
                    "fastapi.FastAPI",
                    "fastapi.APIRouter",
                    "fastapi.applications.FastAPI",
                    "fastapi.routing.APIRouter",
                }:
                    try:
                        prefix = literal(keyword(call, "prefix"), "")
                        if not isinstance(prefix, str):
                            raise ValueError("prefix must be text")
                        if keyword(call, "root_path") is not None:
                            notes.append(
                                f"{path}:{node.lineno}: root_path is deployment metadata; "
                                "verify runtime"
                            )
                    except ValueError:
                        notes.append(f"{path}:{node.lineno}: dynamic router prefix")
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            key = f"{modules[path]}.{target.id}"
                            dependency = keyword(call, "dependencies")
                            routers[key] = Router(
                                key,
                                "app" if factory.endswith("FastAPI") else "router",
                                prefix,
                                sources[path],
                                [ast.unparse(dependency)] if dependency else [],
                            )
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                calls.append((path, node.value, None))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call):
                        calls.append((path, decorator, node))
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                        if inner.func.attr in {"include_router", "mount", "add_api_route"}:
                            notes.append(
                                f"{path}:{inner.lineno}: factory/function routing "
                                "requires confirmation"
                            )
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
                notes.append(f"{path}:{node.lineno}: top-level control flow not evaluated")

    def resolve(key: str) -> str | None:
        if key in routers:
            return key
        matches = [k for k in routers if k.endswith("." + key)]
        return matches[0] if len(matches) == 1 else None

    edges: dict[str, list[tuple[str, str, SourceRef, list[str]]]] = {}
    routes: list[Route] = []
    for path, call, function in calls:
        if not isinstance(call.func, ast.Attribute):
            continue
        action = call.func.attr
        if action not in HTTP | {
            "api_route",
            "websocket",
            "include_router",
            "mount",
            "add_api_route",
            "add_route",
        }:
            continue
        owner = resolve(name(path, call.func.value))
        if owner is None:
            notes.append(f"{path}:{call.lineno}: route receiver outside selected graph")
            continue
        try:
            if action in {"include_router", "mount"}:
                child_expr = (
                    call.args[0]
                    if action == "include_router" and call.args
                    else call.args[1]
                    if action == "mount" and len(call.args) > 1
                    else keyword(call, "router" if action == "include_router" else "app")
                )
                child = resolve(name(path, child_expr)) if child_expr else None
                prefix_expr = (
                    call.args[0]
                    if action == "mount" and call.args
                    else keyword(call, "path" if action == "mount" else "prefix")
                )
                prefix = literal(prefix_expr, "")
                if child is None or not isinstance(prefix, str):
                    raise ValueError("unresolved mounted child/prefix")
                dependency = keyword(call, "dependencies")
                edges.setdefault(owner, []).append(
                    (child, prefix, sources[path], [ast.unparse(dependency)] if dependency else [])
                )
                continue
            path_expr = call.args[0] if call.args else keyword(call, "path")
            route_path = literal(path_expr)
            methods = (
                [action.upper()] if action in HTTP else literal(keyword(call, "methods"), ["GET"])
            )
            if action == "websocket":
                methods = ["WEBSOCKET"]
            if not isinstance(route_path, str) or not route_path.startswith("/"):
                raise ValueError("dynamic or invalid route path")
            if (
                not isinstance(methods, (list, tuple))
                or not methods
                or any(
                    not isinstance(m, str) or m.lower() not in HTTP | {"websocket"} for m in methods
                )
            ):
                raise ValueError("dynamic/unsupported HTTP methods")
            parameters = []
            if function:
                args = [*function.args.posonlyargs, *function.args.args]
                defaults = [None] * (len(args) - len(function.args.defaults)) + list(
                    function.args.defaults
                )
                args += list(function.args.kwonlyargs)
                defaults += list(function.args.kw_defaults)
                for arg, default in zip(args, defaults, strict=True):
                    parameters.append(
                        {
                            "name": arg.arg,
                            "annotation": ast.unparse(arg.annotation) if arg.annotation else None,
                            "default": ast.unparse(default) if default else None,
                            "required": default is None,
                        }
                    )
            options = {k.arg: ast.unparse(k.value) for k in call.keywords if k.arg}
            details = {
                "kind": "implementation",
                "parameters": parameters,
                "options": options,
                "locator": f"line {call.lineno}",
                "authentication": "unresolved: dependency semantics not evaluated",
            }
            routes.append(
                Route(
                    owner,
                    route_path,
                    [m.upper() for m in methods],
                    sources[path],
                    call.lineno,
                    details,
                    action in {"websocket", "add_route"},
                )
            )
        except ValueError as exc:
            notes.append(f"{path}:{call.lineno}: {exc}")

    rows = []
    visited_routes: set[int] = set()
    mounted = {child for children in edges.values() for child, _, _, _ in children}
    roots = [r.key for r in routers.values() if r.kind == "app" and r.key not in mounted]

    def walk(
        key: str,
        prefix: str,
        provenance: list[SourceRef],
        auth: list[str],
        stack: set[str],
        confirmed: bool,
    ) -> None:
        if key in stack:
            notes.append(f"{routers[key].source.path}: cyclic router mount at {key}")
            return
        router = routers[key]
        full_prefix = join_path(prefix, router.prefix).rstrip("/")
        all_sources = {s.source_id: s for s in [*provenance, router.source]}
        for index, route in enumerate(routes):
            if route.owner != key:
                continue
            visited_routes.add(index)
            detail = dict(route.details, inherited_dependencies=[*auth, *router.auth])
            sources_list = list({**all_sources, route.source.source_id: route.source}.values())
            for method in route.methods:
                rows.append(
                    dict(
                        method=method,
                        path=join_path(full_prefix, route.path),
                        original_operation_ids=[],
                        mount_prefix=full_prefix,
                        sources=sources_list,
                        details=detail,
                        status="unsupported"
                        if route.unsupported
                        else "confirmed"
                        if confirmed
                        else "unresolved",
                        reason="Static mounted implementation declaration; auth requires review"
                        if confirmed
                        else "Unattached router; runtime mount unresolved",
                        signature=json.dumps(detail, sort_keys=True),
                    )
                )
        for child, edge_prefix, source, dependencies in edges.get(key, []):
            walk(
                child,
                join_path(full_prefix, edge_prefix),
                [*all_sources.values(), source],
                [*auth, *router.auth, *dependencies],
                stack | {key},
                confirmed,
            )

    for root in roots:
        walk(root, "", [], [], set(), True)
    for index, route in enumerate(routes):
        if index not in visited_routes:
            walk(route.owner, "", [], [], set(), False)
    return rows, sorted(set(notes))


class PythonAstAdapter:
    """Adapter for static discovery of FastAPI endpoints via Python AST."""

    name = "python-ast"
    supported_extensions = (".py",)

    def extract(
        self, snapshots: dict[str, str], sources: dict[str, SourceRef]
    ) -> tuple[list[dict], list[str]]:
        return extract(snapshots, sources)
