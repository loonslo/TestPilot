"""Pluggable language and format adapters for TestPilot."""

from __future__ import annotations

from api_test_agent.adapters.base import (
    AdapterRegistry,
    DiscoveryAdapter,
    get_adapter,
    get_all_supported_extensions,
    list_adapters,
    register_adapter,
    registry,
)
from api_test_agent.adapters.openapi import (
    METHODS,
    OpenAPIAdapter,
    StrictLoader,
    openapi,
    resolve_refs,
)
from api_test_agent.adapters.python_ast import (
    HTTP,
    PythonAstAdapter,
    Route,
    Router,
)
from api_test_agent.adapters.python_ast import (
    extract as extract_python_ast,
)

# Register default discovery adapters
register_adapter(PythonAstAdapter(), "fastapi", "python-ast", "python", "python_ast")
register_adapter(OpenAPIAdapter(), "swagger", "openapi-json", "openapi-yaml")

__all__ = [
    "AdapterRegistry",
    "DiscoveryAdapter",
    "HTTP",
    "METHODS",
    "OpenAPIAdapter",
    "PythonAstAdapter",
    "Route",
    "Router",
    "StrictLoader",
    "extract_python_ast",
    "get_adapter",
    "get_all_supported_extensions",
    "list_adapters",
    "openapi",
    "register_adapter",
    "registry",
    "resolve_refs",
]
