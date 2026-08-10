"""Service layer — the single registry of capabilities.

Every capability the trader exposes is a *query* (read-only) or *command* (state-changing)
registered here exactly once. The REST API, the MCP server, and the CLI are thin adapters
over this registry: one capability, defined once, three doors (docs/architecture.md §3).

Feature milestones add commands/queries here first, then wire the three adapters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from trader.store.db import Database

VERSION = "0.1.0"


@dataclass(frozen=True)
class Capability:
    """One registered service capability."""

    name: str
    kind: str  # "query" | "command"
    handler: Callable[..., Awaitable[Any]]
    summary: str

    @property
    def tool_name(self) -> str:
        """The MCP tool name: queries read as ``get_<name>``, commands keep their name."""
        return f"get_{self.name}" if self.kind == "query" else self.name


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


def build_registry(db: Database | None = None) -> ServiceRegistry:
    """Construct the registry with the capabilities available in this build.

    ``db`` is the shared (possibly not-yet-opened) pool; handlers only touch it per call,
    so the registry can be built before the lifespan opens the pool.
    """
    reg = ServiceRegistry()

    @reg.register("health", kind="query", summary="Liveness, build, and store reachability.")
    async def health() -> dict[str, Any]:
        if db is None:
            db_status = "unconfigured"
        else:
            try:
                await db.fetch_one("SELECT 1")
                db_status = "ok"
            except Exception:
                db_status = "unreachable"
        status = "ok" if db_status == "ok" else "degraded"
        return {"status": status, "service": "trader", "version": VERSION, "db": db_status}

    @reg.register("audit", kind="query", summary="Recent audit events, newest first.")
    async def audit(limit: int = 50) -> list[dict[str, Any]]:
        assert db is not None, "audit requires a store"
        rows = await db.fetch_all(
            "SELECT id, ts, actor, category, action, subject, payload"
            " FROM audit_events ORDER BY ts DESC, id DESC LIMIT %s",
            (min(limit, 500),),
        )
        # ISO-format timestamps so all three surfaces emit the identical JSON shape.
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]

    return reg
