"""FastAPI surface — thin adapter over the service registry, plus the /mcp mount.

One process serves both doors: REST routes and the MCP streamable-HTTP app share the same
registry and DB pool, built once in the lifespan. The mounted MCP sub-app's own lifespan
never runs under FastAPI, so the parent lifespan drives its session manager explicitly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from mcp.server.transport_security import TransportSecuritySettings

from trader.ops.config import Settings, get_settings
from trader.store.db import Database
from trader.store.migrate import run_migrations
from trader.surfaces.api.middleware import ApiKeyMiddleware
from trader.surfaces.mcp.server import build_mcp
from trader.surfaces.services import VERSION, build_registry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.require_auth()

    # The pool object is cheap to build unopened; the lifespan opens/closes it.
    db = Database(settings.database_url)
    registry = build_registry(db)
    mcp = build_mcp(registry)
    # Our ApiKeyMiddleware gates /mcp, and the Spark is reached over the LAN — the MCP
    # localhost-only Host allowlist (DNS-rebinding protection) would reject those clients.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.open()
        await run_migrations(db)  # fails closed: a bad migration refuses boot
        async with mcp.session_manager.run():
            yield
        await db.close()

    app = FastAPI(title="trader", version=VERSION, lifespan=lifespan)
    app.state.db = db
    app.state.registry = registry
    app.add_middleware(
        ApiKeyMiddleware, api_key=settings.api_key, enabled=not settings.auth_disabled
    )

    @app.get("/health")
    async def health() -> dict:
        return await registry.get("health").handler()

    @app.get("/audit")
    async def audit(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
        return await registry.get("audit").handler(limit=limit)

    @app.get("/cache-stats")
    async def cache_stats() -> dict:
        return await registry.get("cache_stats").handler()

    app.mount("/mcp", mcp_app)
    return app


app = create_app()
