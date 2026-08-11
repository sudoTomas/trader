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
    runner = build_runner(db, settings) if settings.loop_enabled else None
    registry = build_registry(db, runner)
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
            if runner is not None:
                await runner.start()  # background task; never blocks /health
            try:
                yield
            finally:
                if runner is not None:
                    await runner.stop()
        await db.close()

    app = FastAPI(title="trader", version=VERSION, lifespan=lifespan)
    app.state.db = db
    app.state.registry = registry
    app.state.runner = runner
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

    @app.get("/risk/status")
    async def risk_status() -> dict:
        return await registry.get("risk_status").handler()

    @app.get("/risk/approvals")
    async def approvals() -> list[dict]:
        return await registry.get("approvals").handler()

    @app.post("/risk/pause")
    async def pause(reason: str = Query(default="operator")) -> dict:
        return await registry.get("pause").handler(reason=reason)

    @app.post("/risk/resume")
    async def resume() -> dict:
        return await registry.get("resume").handler()

    @app.post("/risk/resume-breaker")
    async def resume_breaker(equity: float | None = Query(default=None)) -> dict:
        return await registry.get("resume_breaker").handler(equity=equity)

    @app.get("/decisions")
    async def decisions(limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
        return await registry.get("decisions").handler(limit=limit)

    @app.get("/decisions/{decision_id}")
    async def decision(decision_id: int) -> dict | None:
        return await registry.get("decision").handler(decision_id=decision_id)

    @app.get("/loop/status")
    async def loop_status() -> dict:
        return await registry.get("loop_status").handler()

    @app.get("/positions")
    async def positions() -> list[dict]:
        return await registry.get("positions").handler()

    @app.get("/orders")
    async def orders(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
        return await registry.get("orders").handler(limit=limit)

    @app.post("/broker/reconcile")
    async def reconcile() -> dict:
        return await registry.get("reconcile").handler()

    @app.post("/broker/flatten-all")
    async def flatten_all(confirm: str = Query(default="")) -> dict:
        return await registry.get("flatten_all").handler(confirm=confirm)

    @app.post("/loop/wake")
    async def wake() -> dict:
        return await registry.get("wake_trader").handler()

    app.mount("/mcp", mcp_app)
    return app


def build_runner(db: Database, settings: Settings):
    """Assemble the decide loop from configured providers (opt-in via TRADER_LOOP_ENABLED)."""
    from trader.brokers.alpaca import AlpacaBroker
    from trader.core.symbols import default_registry
    from trader.data.composite import build_market_data
    from trader.llm.registry import build_llm_registry
    from trader.llm.usage import UsageMeter
    from trader.loop.executor import Executor
    from trader.loop.runner import LoopRunner
    from trader.risk.guard import RiskGuard
    from trader.risk.limits import limits_from_settings
    from trader.store.repos import UsageRepo

    symbols_registry = default_registry()
    tickers = [t.strip() for t in settings.loop_symbols.split(",") if t.strip()]

    # A broker is attached only when leaving shadow mode — a shadow build has no way to
    # reach a venue at all, not merely a flag telling it not to.
    executor = None
    if not settings.shadow_mode:
        broker = AlpacaBroker.from_settings(settings)
        executor = Executor(db, broker)

    return LoopRunner(
        db=db,
        market_data=build_market_data(settings),
        llm=build_llm_registry(settings, UsageMeter(UsageRepo(db))),
        guard=RiskGuard(
            db, limits_from_settings(settings), live_enabled=settings.alpaca_live_enabled
        ),
        symbols=[symbols_registry.get(t) for t in tickers],
        shadow=settings.shadow_mode,
        tick_seconds=settings.loop_tick_seconds,
        equity=settings.shadow_equity,
        executor=executor,
    )


app = create_app()
