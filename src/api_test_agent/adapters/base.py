"""Base adapter interface and registry for discovery and language/framework integration."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from api_test_agent.tasks.models import SourceRef


@runtime_checkable
class DiscoveryAdapter(Protocol):
    """Protocol for static route / operation discovery adapters."""

    name: str
    supported_extensions: tuple[str, ...]

    def extract(
        self, snapshots: dict[str, str], sources: dict[str, SourceRef]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Extract candidate operations from snapshots and return (rows, warnings)."""
        ...


class AdapterRegistry:
    """Registry for pluggable language and framework discovery adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, DiscoveryAdapter] = {}

    def register(self, adapter: DiscoveryAdapter, *aliases: str) -> None:
        self._adapters[adapter.name] = adapter
        for alias in aliases:
            self._adapters[alias] = adapter

    def get(self, name: str) -> DiscoveryAdapter | None:
        return self._adapters.get(name)

    def list_adapters(self) -> list[str]:
        return sorted(set(self._adapters.keys()))

    def get_all_supported_extensions(self) -> set[str]:
        exts: set[str] = set()
        for adapter in self._adapters.values():
            exts.update(adapter.supported_extensions)
        return exts


registry = AdapterRegistry()


def register_adapter(adapter: DiscoveryAdapter, *aliases: str) -> None:
    registry.register(adapter, *aliases)


def get_adapter(name: str) -> DiscoveryAdapter | None:
    return registry.get(name)


def list_adapters() -> list[str]:
    return registry.list_adapters()


def get_all_supported_extensions() -> set[str]:
    return registry.get_all_supported_extensions()
