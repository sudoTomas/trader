"""Service layer — the single registry of capabilities.

Every capability the trader exposes is a *query* (read-only) or *command* (state-changing)
registered here exactly once. The REST API, the MCP server, and the CLI are thin adapters
over this registry: one capability, defined once, three doors (docs/architecture.md §3).

The walking skeleton ships one query — ``health`` — proving the pattern end to end across
all three surfaces. Feature milestones add commands/queries here first, then wire the three
adapters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Capability:
    """One registered service capability."""

    name: str
    kind: str  # "query" | "command"
    handler: Callable[..., Awaitable[Any]]
    summary: str


@dataclass
class ServiceRegistry:
    """Holds the capability set. One instance is built at startup and shared by all surfaces."""

    _caps: dict[str, Capability] = field(default_factory=dict)

    def register(self, name: str, kind: str, summary: str):
        def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            if name in self._caps:
                raise ValueError(f"capability already registered: {name}")
            self._caps[name] = Capability(name=name, kind=kind, handler=fn, summary=summary)
            return fn

        return deco

    def get(self, name: str) -> Capability:
        return self._caps[name]

    def all(self) -> list[Capability]:
        return list(self._caps.values())


def build_registry() -> ServiceRegistry:
    """Construct the registry with the capabilities available in this build."""
    reg = ServiceRegistry()

    @reg.register("health", kind="query", summary="Liveness and build status.")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "trader", "version": "0.1.0"}

    return reg
