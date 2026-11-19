"""Discover literal Spring MVC controller mappings without running the application."""

from __future__ import annotations

import json
import re
from itertools import product
from pathlib import PurePosixPath

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from api_test_agent.tasks.models import SourceRef

BIND = "org.springframework.web.bind.annotation"
CONTROLLER = "org.springframework.stereotype.Controller"
SHORTCUTS = dict(
    zip(
        ("GetMapping", "PostMapping", "PutMapping", "PatchMapping", "DeleteMapping"),
        ("GET", "POST", "PUT", "PATCH", "DELETE"),
        strict=True,
    )
)
METHODS = {*SHORTCUTS.values(), "HEAD", "OPTIONS", "TRACE"}
PARAMETERS = {
    "PathVariable": "path",
    "RequestParam": "query",
    "RequestHeader": "header",
    "RequestBody": "body",
}
LANGUAGE = Language(tree_sitter_java.language())


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None and node.text is not None else ""


def _annotations(node: Node, imports: set[str]) -> list[tuple[str, Node]]:
    modifiers = next((child for child in node.named_children if child.type == "modifiers"), None)
    result = []
    for annotation in modifiers.named_children if modifiers else ():
        if annotation.type not in {"annotation", "marker_annotation"}:
            continue
        name = re.sub(r"\s+", "", _text(annotation.child_by_field_name("name")))
        short = name.rsplit(".", 1)[-1]
        qualified = CONTROLLER if short == "Controller" else f"{BIND}.{short}"
        package = qualified.rsplit(".", 1)[0]
        if name == qualified or (name == short and {qualified, f"{package}.*"} & imports):
            result.append((short, annotation))
    return result


def _arguments(annotation: Node) -> dict[str, Node]:
    arguments = annotation.child_by_field_name("arguments")
    values = {}
    for child in arguments.named_children if arguments else ():
        if child.type == "element_value_pair":
            value = child.child_by_field_name("value")
            if value is not None:
                values[_text(child.child_by_field_name("key"))] = value
        elif child.type not in {"line_comment", "block_comment"}:
            values["value"] = child
    return values


def _values(node: Node) -> list[Node]:
    if node.type != "element_value_array_initializer":
        return [node]
    return [c for c in node.named_children if c.type not in {"line_comment", "block_comment"}]


def _mapping(name: str, annotation: Node) -> tuple[list[str], set[str], bool]:
    args = _arguments(annotation)
    path = args.get("path") or args.get("value")
    if "path" in args and "value" in args:
        raise ValueError("path and value aliases require review")
    paths = []
    for part in _values(path) if path is not None else ():
        if part.type != "string_literal":
            raise ValueError("dynamic mapping path")
        literal = json.loads(_text(part))
        if re.search(r"[$*?#\s\\]", literal):
            raise ValueError("mapping path pattern or placeholder requires review")
        paths.append(literal)
    methods = {SHORTCUTS[name]} if name in SHORTCUTS else set()
    if "method" in args:
        for value in _values(args["method"]):
            parts = re.sub(r"\s+", "", _text(value)).split(".")
            if parts[-1] not in METHODS or (len(parts) > 1 and parts[-2] != "RequestMethod"):
                raise ValueError("dynamic HTTP method")
            methods.add(parts[-1])
    return list(dict.fromkeys(paths)) or [""], methods, bool({"params", "headers"} & args.keys())


def _parameters(method: Node, imports: set[str]) -> list[dict]:
    params = method.child_by_field_name("parameters")
    result = []
    for param in params.named_children if params else ():
        for label, annotation in _annotations(param, imports):
            if label not in PARAMETERS:
                continue
            args = _arguments(annotation)
            name = args.get("name") or args.get("value")
            parameter_name = _text(param.child_by_field_name("name"))
            if name is not None:
                if name.type != "string_literal":
                    continue
                try:
                    parameter_name = json.loads(_text(name))
                except ValueError:
                    continue
            if label == "RequestBody":
                parameter_name = "$"
            if parameter_name:
                result.append(
                    {
                        "name": parameter_name,
                        "in": PARAMETERS[label],
                        "annotation": _text(param.child_by_field_name("type")),
                        "required": _text(args.get("required")) != "false"
                        and "defaultValue" not in args,
                    }
                )
    return result


class JavaSpringAdapter:
    name = "java-spring"
    supported_extensions = (".java",)

    def extract(
        self, snapshots: dict[str, str], sources: dict[str, SourceRef]
    ) -> tuple[list[dict], list[str]]:
        parser = Parser(LANGUAGE)
        rows: list[dict] = []
        warnings: list[str] = []
        for path, content in snapshots.items():
            if PurePosixPath(path).suffix.lower() != ".java":
                continue
            root = parser.parse(content.encode("utf-8")).root_node
            if root.has_error:
                warnings.append(f"{path}: Java syntax could not be parsed")
                continue
            imports = {
                re.sub(r"\s+", "", _text(child)).removeprefix("import").removesuffix(";")
                for child in root.named_children
                if child.type == "import_declaration"
            }
            pending = [child for child in root.named_children if child.type == "class_declaration"]
            while pending:
                cls = pending.pop()
                body = cls.child_by_field_name("body")
                if body is None:
                    continue
                members = body.named_children
                pending.extend(member for member in members if member.type == "class_declaration")
                annotations = dict(_annotations(cls, imports))
                if not {"Controller", "RestController"} & annotations.keys():
                    continue
                try:
                    prefixes, class_methods, class_conditions = (
                        _mapping("RequestMapping", annotations["RequestMapping"])
                        if "RequestMapping" in annotations
                        else ([""], set(), False)
                    )
                except ValueError as exc:
                    warnings.append(f"{path}:{cls.start_point.row + 1}: {exc}")
                    continue
                for method in members:
                    if method.type == "method_declaration":
                        _handler_rows(
                            method,
                            imports,
                            sources[path],
                            prefixes,
                            class_methods,
                            class_conditions,
                            rows,
                            warnings,
                        )
        return rows, warnings


def _handler_rows(
    method: Node,
    imports: set[str],
    source: SourceRef,
    prefixes: list[str],
    class_methods: set[str],
    class_conditions: bool,
    rows: list[dict],
    warnings: list[str],
) -> None:
    mappings = [
        (name, node)
        for name, node in _annotations(method, imports)
        if name == "RequestMapping" or name in SHORTCUTS
    ]
    if not mappings:
        return
    location = f"{source.path}:{method.start_point.row + 1}"
    try:
        if len(mappings) != 1:
            raise ValueError("multiple mapping annotations require review")
        paths, method_verbs, method_conditions = _mapping(*mappings[0])
        verbs = class_methods | method_verbs
        if not verbs:
            raise ValueError("mapping declares no specific HTTP method")
        if len(prefixes) * len(paths) * len(verbs) > 500:
            raise ValueError("mapping expansion exceeds static discovery limit")
    except ValueError as exc:
        warnings.append(f"{location}: {exc}")
        return
    details = {
        "kind": "implementation",
        "framework": "java-spring",
        "parameters": _parameters(method, imports),
        "authentication": "unresolved: Spring Security configuration not evaluated",
    }
    conditional = class_conditions or method_conditions
    for prefix, leaf, verb in product(prefixes, paths, sorted(verbs)):
        mount = "/" + prefix.strip("/") if prefix.strip("/") else ""
        route = f"{mount}/{leaf.lstrip('/')}" if leaf else mount or "/"
        rows.append(
            {
                "method": verb,
                "path": route,
                "mount_prefix": mount,
                "original_operation_ids": [],
                "sources": [source],
                "details": details,
                "status": "unresolved" if conditional else "confirmed",
                "reason": "Spring header or parameter conditions require review"
                if conditional
                else "Spring MVC static mapping; runtime configuration requires review",
                "signature": json.dumps(details, sort_keys=True),
            }
        )
