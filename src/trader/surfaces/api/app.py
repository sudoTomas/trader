"""FastAPI surface — thin adapter over the service registry, plus the /mcp mounts.

One process serves every door: REST routes and the MCP streamable-HTTP apps share the same
registry and DB pool, built once in the lifespan. The mounted MCP sub-apps' own lifespans
never run under FastAPI, so the parent lifespan drives their session managers explicitly.
Two MCP mounts: ``/mcp`` (everything, operator key) and ``/mcp-capture`` (the four
research-capture tools; the capture key's only MCP door — #8).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from trader.ops.config import Settings, get_settings
from trader.store.db import Database
from trader.store.migrate import run_migrations
from trader.surfaces.api.middleware import ApiKeyMiddleware
from trader.surfaces.mcp.server import CAPTURE_CAPABILITIES, build_mcp
from trader.surfaces.services import VERSION, build_registry


class ResearchNoteIn(BaseModel):
    """Capture payload for ``POST /research/notes``. The body is stored verbatim."""

    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    tags: list[str] = []
    asset_classes: list[str] = []
    symbols: list[str] = []
    source: str | None = None
    pinned: bool = False
    stale_days: int | None = Field(default=None, ge=0)
    # When the research was CREATED (ISO-8601, tz-aware); submission time is recorded
    # separately as created_at. Freshness anchors on this when given.
    authored_at: str | None = None


class XaiProbeIn(BaseModel):
    """``POST /xai/probe`` body. Every field optional: the service fills the #7 matrix
    defaults (models × both endpoints × none/x_search/web_search, credential auto)."""

    models: list[str] | None = None
    endpoints: list[str] | None = None
    tools: list[str] | None = None  # "none" | "x_search" | "web_search"
    credential: str = "auto"  # auto | oauth | api_key


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.require_auth()

    # The pool object is cheap to build unopened; the lifespan opens/closes it.
    db = Database(settings.database_url)
    sentiment = build_sentiment(db, settings)
    desk = build_desk(db, settings)
    runner = build_runner(db, settings, sentiment, desk) if settings.loop_enabled else None
    mirror = build_mirror(db, settings)
    compiler = build_compiler(db, settings, mirror)
    briefs = build_briefs(db, settings)
    evaluator = build_evaluator(db, settings, desk)
    registry = build_registry(
        db,
        runner,
        compiler,
        mirror,
        sentiment,
        evaluator,
        briefs,
        xai_http=build_xai_http(settings),
        screener=build_screener_data(db, settings),
    )
    mcp = build_mcp(registry)
    capture_mcp = build_mcp(registry, only=CAPTURE_CAPABILITIES, name="trader-capture")
    # Our ApiKeyMiddleware gates both mounts, and the host is reached over the LAN — the
    # MCP localhost-only Host allowlist (DNS-rebinding protection) would reject those clients.
    transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/", transport_security=transport_security
    )
    capture_mcp_app = capture_mcp.streamable_http_app(
        streamable_http_path="/", transport_security=transport_security
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.open()
        await run_migrations(db)  # fails closed: a bad migration refuses boot
        async with mcp.session_manager.run(), capture_mcp.session_manager.run():
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
        ApiKeyMiddleware,
        api_key=settings.api_key,
        capture_api_key=settings.capture_api_key,
        enabled=not settings.auth_disabled,
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

    @app.post("/risk/approvals/{approval_id}/resolve")
    async def resolve_approval(
        approval_id: int,
        approved: bool = Query(),
        resolved_by: str = Query(default="operator"),
    ) -> dict:
        return await registry.get("resolve_approval").handler(
            approval_id=approval_id, approved=approved, resolved_by=resolved_by
        )

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

    @app.get("/usage")
    async def usage(
        days: int = Query(default=14, ge=1, le=366),
        role: str | None = Query(default=None),
        day: str | None = Query(default=None, description="YYYY-MM-DD; overrides days"),
    ) -> dict:
        try:
            return await registry.get("usage").handler(days=days, role=role, day=day)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/broker/reconcile")
    async def reconcile() -> dict:
        return await registry.get("reconcile").handler()

    @app.post("/broker/adopt")
    async def adopt_venue_positions(confirm: str = Query(default="")) -> dict:
        return await registry.get("adopt_venue_positions").handler(confirm=confirm)

    @app.post("/broker/flatten-all")
    async def flatten_all(confirm: str = Query(default="")) -> dict:
        return await registry.get("flatten_all").handler(confirm=confirm)

    @app.post("/loop/wake")
    async def wake() -> dict:
        return await registry.get("wake_trader").handler()

    @app.post("/research/notes")
    async def submit_research_note(note: ResearchNoteIn) -> dict:
        try:
            return await registry.get("submit_research_note").handler(**note.model_dump())
        except ValueError as exc:  # blank title/body past pydantic (whitespace-only)
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/research/notes")
    async def research_notes(
        include_stale: bool = Query(default=False),
        include_retired: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict]:
        return await registry.get("research_notes").handler(
            include_stale=include_stale, include_retired=include_retired, limit=limit
        )

    @app.post("/research/notes/{note_id}/retire")
    async def retire_note(note_id: int) -> dict:
        return await registry.get("retire_note").handler(note_id=note_id)

    @app.get("/research/view")
    async def macro_view(version: int | None = Query(default=None, ge=1)) -> dict | None:
        return await registry.get("macro_view").handler(version=version)

    @app.post("/research/view/compile")
    async def compile_macro_view() -> dict | None:
        return await registry.get("compile_macro_view").handler()

    @app.post("/research/mirror/export")
    async def export_research_mirror() -> dict:
        return await registry.get("export_research_mirror").handler()

    @app.get("/research/briefs")
    async def brief(
        symbol: str | None = Query(default=None),
        version: int | None = Query(default=None, ge=1),
    ) -> dict | None:
        return await registry.get("brief").handler(symbol=symbol, version=version)

    @app.post("/research/briefs/compile")
    async def compile_brief(symbol: str = Query()) -> dict:
        return await registry.get("compile_brief").handler(symbol=symbol)

    @app.get("/research/sentiment")
    async def sentiment_history(
        symbol: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict]:
        return await registry.get("sentiment").handler(symbol=symbol, limit=limit)

    @app.post("/research/sentiment/fetch")
    async def fetch_x_sentiment(symbol: str = Query()) -> dict:
        return await registry.get("fetch_x_sentiment").handler(symbol=symbol)

    @app.post("/eval/replay")
    async def run_replay_eval(
        symbol: str = Query(),
        start: str = Query(),
        end: str = Query(),
        label: str = Query(default="replay"),
        desk: bool = Query(default=False),
    ) -> dict:
        try:
            return await registry.get("run_replay_eval").handler(
                symbol=symbol, start=start, end=end, label=label, desk=desk
            )
        except (ValueError, KeyError) as exc:  # bad window / unknown symbol
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/eval/runs")
    async def eval_runs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
        return await registry.get("eval_runs").handler(limit=limit)

    @app.get("/eval/runs/{run_id}")
    async def eval_run(run_id: int) -> dict | None:
        return await registry.get("eval_run").handler(run_id=run_id)

    @app.post("/xai/probe")
    async def probe_xai(body: XaiProbeIn) -> dict:
        try:
            return await registry.get("probe_xai").handler(**body.model_dump())
        except ValueError as exc:  # unknown endpoint / tool / credential
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/screener")
    async def screener(
        universe: str = Query(default="sp100"),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> dict:
        from trader.data.base import MarketDataError

        try:
            return await registry.get("screener").handler(universe=universe, limit=limit)
        except KeyError as exc:  # unknown universe
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except MarketDataError as exc:  # the venue could not answer
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    app.mount("/mcp", mcp_app)
    app.mount("/mcp-capture", capture_mcp_app)
    return app


def build_sentiment(db: Database, settings: Settings):
    """X sentiment service — None without an xAI credential path (x_search is xAI-only)."""
    import httpx

    from trader.llm.xai import ManagedBearerAuth
    from trader.llm.xai_auth import build_manager
    from trader.research.sentiment import SentimentService, XSentimentClient

    manager = build_manager(settings)
    if manager is None and not settings.xai_api_key:
        return None
    http = httpx.AsyncClient(
        base_url=settings.xai_base_url,
        auth=ManagedBearerAuth(manager, settings.xai_api_key),
        timeout=120.0,
    )
    handles = [h.strip() for h in settings.sentiment_x_handles.split(",") if h.strip()]
    client = XSentimentClient(http, model=settings.xai_model, allowed_x_handles=handles)
    return SentimentService(db, client, daily_call_cap=settings.sentiment_daily_call_cap)


def build_evaluator(db: Database, settings: Settings, desk=None):
    """The replay eval harness (offline; Nemotron decide role by default — free).
    None without market-data credentials — every builder here must degrade gracefully,
    because ``app = create_app()`` runs at import time (uvicorn's entrypoint). The desk
    is wired in every build so desk-on replays work while the live desk stays dark."""
    from trader.data.composite import build_market_data
    from trader.eval.replay import ReplayHarness
    from trader.llm.registry import build_llm_registry
    from trader.llm.usage import UsageMeter
    from trader.store.repos import UsageRepo

    try:
        provider = build_market_data(settings)
    except ValueError:  # no Alpaca keys in this environment (e.g. CI)
        return None
    return ReplayHarness(
        db, build_llm_registry(settings, UsageMeter(UsageRepo(db))), provider, desk=desk
    )


def build_screener_data(db: Database, settings: Settings):
    """The screener's multi-symbol daily-bars provider (Alpaca free tier, sip + delayed
    end). None without Alpaca keys — the query then answers honestly with an empty
    shortlist instead of failing at import time. ``db`` is unused: the screener stores
    nothing (docs/lessons.md#no-market-data-storage)."""
    from trader.data.alpaca import AlpacaData

    if not (settings.alpaca_api_key_id and settings.alpaca_api_secret):
        return None
    return AlpacaData(settings.alpaca_api_key_id, settings.alpaca_api_secret)


def build_desk(db: Database, settings: Settings):
    """The decision desk — built in every store-backed build (replay needs it); the LIVE
    loop only receives it when TRADER_DESK_ENABLED=true (see create_app)."""
    from trader.desk.desk import Desk
    from trader.llm.registry import build_llm_registry
    from trader.llm.usage import UsageMeter
    from trader.store.repos import UsageRepo

    return Desk(
        build_llm_registry(settings, UsageMeter(UsageRepo(db))),
        usage=UsageRepo(db),
        critic_daily_usd=settings.desk_critic_daily_usd,
    )


def build_mirror(db: Database, settings: Settings):
    """The one-way markdown mirror; TRADER_RESEARCH_MIRROR_DIR="" disables it."""
    from trader.research.mirror import MarkdownMirror

    if not settings.research_mirror_dir:
        return None
    return MarkdownMirror(db, settings.research_mirror_dir)


def build_compiler(db: Database, settings: Settings, mirror=None):
    """The Macro View compiler — present in every store-backed build, loop or not."""
    from trader.llm.registry import build_llm_registry
    from trader.llm.usage import UsageMeter
    from trader.research.compiler import MacroViewCompiler
    from trader.store.repos import UsageRepo

    return MacroViewCompiler(
        db,
        build_llm_registry(settings, UsageMeter(UsageRepo(db))),
        debounce_seconds=settings.research_compile_debounce_seconds,
        on_compiled=mirror.export_quietly if mirror is not None else None,
    )


def build_briefs(db: Database, settings: Settings):
    """The per-candidate brief compiler — present in every store-backed build. The lane
    into the decide prompt is gated separately (TRADER_BRIEFS_ENABLED, see build_runner)."""
    from trader.llm.registry import build_llm_registry
    from trader.llm.usage import UsageMeter
    from trader.research.briefs import BriefCompiler
    from trader.store.repos import UsageRepo

    return BriefCompiler(
        db,
        build_llm_registry(settings, UsageMeter(UsageRepo(db))),
        symbols=[t.strip() for t in settings.loop_symbols.split(",") if t.strip()],
        debounce_seconds=settings.research_compile_debounce_seconds,
    )


def build_runner(db: Database, settings: Settings, sentiment=None, desk=None):
    """Assemble the decide loop from configured providers (opt-in via TRADER_LOOP_ENABLED)."""
    from trader.brokers.alpaca import AlpacaBroker
    from trader.core.symbols import default_registry
    from trader.data.composite import build_market_data
    from trader.llm.registry import build_llm_registry
    from trader.llm.usage import UsageMeter
    from trader.loop.executor import Executor
    from trader.loop.runner import LoopRunner
    from trader.loop.scheduler import SignalScheduler, TriggerConfig
    from trader.risk.guard import RiskGuard
    from trader.risk.limits import limits_from_settings
    from trader.store.repos import UsageRepo

    symbols_registry = default_registry()
    tickers = [t.strip() for t in settings.loop_symbols.split(",") if t.strip()]
    symbols = [symbols_registry.get(t) for t in tickers]

    # A broker is attached only when leaving shadow mode — a shadow build has no way to
    # reach a venue at all, not merely a flag telling it not to. The same client feeds
    # account equity (#13); shadow builds keep settings.shadow_equity.
    executor = None
    broker = None
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
        symbols=symbols,
        shadow=settings.shadow_mode,
        tick_seconds=settings.loop_tick_seconds,
        equity=settings.shadow_equity,
        broker=broker,
        scheduler=SignalScheduler(
            symbols,
            TriggerConfig(
                sentiment_enabled=settings.sentiment_trigger_enabled,
                sentiment_swing=settings.sentiment_swing_threshold,
            ),
        ),
        executor=executor,
        sentiment=sentiment,
        sentiment_poll_seconds=settings.sentiment_poll_seconds,
        shadow_symbols={t.strip() for t in settings.shadow_symbols.split(",") if t.strip()},
        briefs_enabled=settings.briefs_enabled,
        desk=desk if settings.desk_enabled else None,
    )


def build_xai_http(settings: Settings):
    """The service's authenticated api.x.ai client for the operator diagnostics
    (`trader xai probe`): the roles' credential path, None without any xAI credential."""
    from trader.llm.xai import xai_http_client

    return xai_http_client(settings)


app = create_app()
