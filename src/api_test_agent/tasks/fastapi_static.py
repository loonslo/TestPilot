"""Resolve the supported FastAPI routing graph without importing user modules.

Unsupported control flow and expressions are recorded, never evaluated.
(Re-exported from api_test_agent.adapters.python_ast for backwards compatibility).
"""

from __future__ import annotations

from api_test_agent.adapters.python_ast import (
    HTTP,
    PythonAstAdapter,
    Route,
    Router,
    extract,
    join_path,
    keyword,
    literal,
    module_name,
)

__all__ = [
    "HTTP",
    "PythonAstAdapter",
    "Route",
    "Router",
    "extract",
    "join_path",
    "keyword",
    "literal",
    "module_name",
]
