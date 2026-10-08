"""FastAPI surface — thin adapter over the service registry, plus the /mcp mounts.

One process serves every door: REST routes and the MCP streamable-HTTP apps share the same
registry and DB pool, built once in the lifespan. The mounted MCP sub-apps' own lifespans
never run under FastAPI, so the parent lifespan drives their session managers explicitly.
Two MCP mounts: ``/mcp`` (everything, operator key) and ``/mcp-capture`` (the four
research-capture tools; the capture key's only MCP door — #8).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from trader.llm.caps import CapError
from trader.llm.profiles import ProfileError, ProfileNotFound
from trader.llm.providers import ProviderError, ProviderNotFound
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


class ProviderIn(BaseModel):
    """``POST /providers`` body. The key travels here, never in a query string (those get
    logged); it is stored encrypted and never returned."""

    name: str
    preset: str
    api_key: str | None = None
    base_url: str | None = None


class ProviderKeyIn(BaseModel):
    api_key: str = Field(min_length=1)


class ProviderRenameIn(BaseModel):
    new_name: str


class ModelPriceIn(BaseModel):
    """A manual price, USD per 1M tokens. The model travels in the body: ids carry '/'."""

    model: str
    price_in: float
    price_out: float


class ModelIn(BaseModel):
    model: str


class ProfileIn(BaseModel):
    name: str


class ProfileNameIn(BaseModel):
    new_name: str


class ChainEntryIn(BaseModel):
    provider: str
    model: str


class ChainIn(BaseModel):
    """A Role's Chain, first entry serves; empty turns the confirm Role off."""

    chain: list[ChainEntryIn]


class RevertIn(BaseModel):
    version: int


class CapIn(BaseModel):
    daily_usd: float


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
    # ONE xAI OAuth credential manager per process: every builder below that reaches
    # api.x.ai shares it, so a single lock spends the single-use rotating refresh token
    # (#63, docs/lessons.md#oauth-refresh-margin). None without TRADER_XAI_TOKEN_KEY.
    xai_manager = build_xai_manager(settings)
    providers = build_providers(db, settings)
    caps = build_caps(db, settings)
    profiles = build_profiles(db, settings, providers, caps)
    # ONE registry per process (#107): it reads the active Profile and the Spend caps per
    # call, so a switch or a cap reaches the loop, desk, compiler, briefs and replays at once.
    llm = build_llm(db, settings, profiles, caps, xai_manager=xai_manager)
    sentiment = build_sentiment(db, settings, xai_manager=xai_manager, profiles=profiles)
    desk = build_desk(db, settings, llm=llm)
    runner = build_runner(db, settings, sentiment, desk, llm=llm) if settings.loop_enabled else None
    mirror = build_mirror(db, settings)
    compiler = build_compiler(db, settings, mirror, llm=llm)
    briefs = build_briefs(db, settings, llm=llm)
    evaluator = build_evaluator(db, settings, desk, llm=llm)
    xai_http = build_xai_http(settings, xai_manager=xai_manager)
    registry = build_registry(
        db,
        runner,
        compiler,
        mirror,
        sentiment,
        evaluator,
        briefs,
        xai_http=xai_http,
        screener=build_screener_data(db, settings),
        providers=providers,
        profiles=profiles,
        caps=caps,
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
    app.state.xai_manager = xai_manager
    app.state.xai_http = xai_http
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

    @app.exception_handler(ProviderNotFound)
    async def provider_not_found(_: Request, exc: ProviderNotFound) -> JSONResponse:
        return JSONResponse({"detail": f"no Provider named {exc.args[0]!r}"}, status_code=404)

    @app.exception_handler(ProviderError)
    async def provider_refused(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/providers")
    async def providers() -> list[dict]:
        return await registry.get("providers").handler()

    @app.get("/providers/presets")
    async def provider_presets() -> dict:
        return await registry.get("provider_presets").handler()

    @app.get("/providers/discover")
    async def discover_providers() -> list[dict]:
        return await registry.get("discover_providers").handler()

    @app.get("/providers/{name}/models")
    async def provider_models(name: str) -> list[dict]:
        return await registry.get("provider_models").handler(name=name)

    @app.post("/providers")
    async def add_provider(provider: ProviderIn) -> dict:
        return await registry.get("add_provider").handler(**provider.model_dump())

    @app.post("/providers/{name}/key")
    async def set_provider_key(name: str, body: ProviderKeyIn) -> dict:
        return await registry.get("set_provider_key").handler(name=name, api_key=body.api_key)

    @app.post("/providers/{name}/rename")
    async def rename_provider(name: str, body: ProviderRenameIn) -> dict:
        return await registry.get("rename_provider").handler(name=name, new_name=body.new_name)

    @app.post("/providers/{name}/prices")
    async def set_model_price(name: str, body: ModelPriceIn) -> dict:
        return await registry.get("set_model_price").handler(name=name, **body.model_dump())

    @app.post("/providers/{name}/prices/clear")
    async def clear_model_price(name: str, body: ModelIn) -> dict:
        return await registry.get("clear_model_price").handler(name=name, model=body.model)

    @app.delete("/providers/{name}")
    async def remove_provider(name: str) -> dict:
        return await registry.get("remove_provider").handler(name=name)

    @app.exception_handler(ProfileNotFound)
    async def profile_not_found(_: Request, exc: ProfileNotFound) -> JSONResponse:
        return JSONResponse({"detail": f"no Profile {exc.args[0]!r}"}, status_code=404)

    @app.exception_handler(ProfileError)
    async def profile_refused(_: Request, exc: ProfileError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(CapError)
    async def cap_refused(_: Request, exc: CapError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/caps")
    async def spend_caps() -> dict:
        return await registry.get("spend_caps").handler()

    @app.put("/caps/{role}")
    async def set_spend_cap(role: str, body: CapIn) -> dict:
        return await registry.get("set_spend_cap").handler(role=role, daily_usd=body.daily_usd)

    @app.delete("/caps/{role}")
    async def clear_spend_cap(role: str) -> dict:
        return await registry.get("clear_spend_cap").handler(role=role)

    @app.get("/profiles")
    async def profiles() -> list[dict]:
        return await registry.get("profiles").handler()

    @app.get("/roles")
    async def role_routes() -> dict:
        return await registry.get("role_routes").handler()

    @app.post("/roles/{role}/probe")
    async def probe_role(role: str) -> dict:
        return await registry.get("probe_role").handler(role=role)

    @app.get("/profiles/{name}")
    async def profile(name: str) -> dict:
        return await registry.get("profile").handler(name=name)

    @app.post("/profiles")
    async def create_profile(body: ProfileIn) -> dict:
        return await registry.get("create_profile").handler(name=body.name)

    @app.post("/profiles/{name}/rename")
    async def rename_profile(name: str, body: ProfileNameIn) -> dict:
        return await registry.get("rename_profile").handler(name=name, new_name=body.new_name)

    @app.post("/profiles/{name}/duplicate")
    async def duplicate_profile(name: str, body: ProfileNameIn) -> dict:
        return await registry.get("duplicate_profile").handler(name=name, new_name=body.new_name)

    @app.put("/profiles/{name}/roles/{role}")
    async def set_role_chain(name: str, role: str, body: ChainIn) -> dict:
        chain = [entry.model_dump() for entry in body.chain]
        return await registry.get("set_role_chain").handler(name=name, role=role, chain=chain)

    @app.delete("/profiles/{name}/roles/{role}")
    async def reset_role(name: str, role: str) -> dict:
        return await registry.get("reset_role").handler(name=name, role=role)

    @app.post("/profiles/{name}/activate")
    async def activate_profile(name: str) -> dict:
        return await registry.get("activate_profile").handler(name=name)

    @app.post("/profiles/{name}/revert")
    async def revert_profile(name: str, body: RevertIn) -> dict:
        return await registry.get("revert_profile").handler(name=name, version=body.version)

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

    @app.get("/fred")
    async def fred(
        series: str = Query(default="VIXCLS", description="FRED series id."),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict:
        return await registry.get("fred").handler(series=series, limit=limit)

    @app.post("/eval/replay")
    async def run_replay_eval(
        symbol: str = Query(),
        start: str = Query(),
        end: str = Query(),
        label: str = Query(default="replay"),
        desk: bool = Query(default=False),
        briefs: bool = Query(default=True, description="Include the brief lane."),
        macro_view: bool = Query(default=True, description="Include the Macro View lane."),
        exit_review: bool | None = Query(
            default=None,
            description="Render the held-position block (#34); unset follows the setting.",
        ),
        seed_qty: float | None = Query(
            default=None, description="Seed a held position: signed quantity (with seed_entry)."
        ),
        seed_entry: float | None = Query(
            default=None, description="Seed a held position: average entry price (with seed_qty)."
        ),
        seed_opened_at: str | None = Query(
            default=None, description="Optional seed open time (ISO-8601); default window start."
        ),
    ) -> dict:
        seed: dict | None = None
        if (seed_qty is None) != (seed_entry is None):
            raise HTTPException(
                status_code=422, detail="seed_qty and seed_entry must be given together"
            )
        if seed_qty is not None:
            seed = {"qty": seed_qty, "avg_entry_price": seed_entry}
            if seed_opened_at is not None:
                seed["opened_at"] = seed_opened_at
        try:
            return await registry.get("run_replay_eval").handler(
                symbol=symbol,
                start=start,
                end=end,
                label=label,
                desk=desk,
                briefs=briefs,
                macro_view=macro_view,
                exit_review=exit_review,
                exit_review_seed=seed,
            )
        except (ValueError, KeyError) as exc:  # bad window / unknown symbol / bad seed
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


def build_xai_manager(settings: Settings):
    """The process-wide SuperGrok OAuth credential manager — built ONCE here and handed to
    every builder below (``xai_manager=``). None without ``TRADER_XAI_TOKEN_KEY``; the
    builders then run the same ``build_manager`` fallback they always did (None → API key)."""
    from trader.llm.xai_auth import build_manager

    return build_manager(settings)


def build_sentiment(db: Database, settings: Settings, *, xai_manager=None, profiles=None):
    """X sentiment service (x_search is xAI-only). Its ``.env`` client rides the roles'
    authenticated client (OAuth-then-key) over the shared manager; the active Profile's
    sentiment Chain overrides it per poll. None only when neither can ever exist."""
    from trader.llm.xai import xai_http_client
    from trader.research.sentiment import SentimentService, XSentimentClient

    http = xai_http_client(settings, manager=xai_manager)
    if http is None and profiles is None:
        return None
    client = None
    if http is not None:
        client = XSentimentClient(
            http, model=settings.xai_model, allowed_x_handles=sentiment_handles(settings)
        )
    return SentimentService(
        db, client, daily_call_cap=settings.sentiment_daily_call_cap, profiles=profiles
    )


def sentiment_handles(settings: Settings) -> list[str]:
    return [h.strip() for h in settings.sentiment_x_handles.split(",") if h.strip()]


def build_llm(db: Database, settings: Settings, profiles=None, caps=None, *, xai_manager=None):
    """The process's one metered LLM registry: ``.env`` chains, overridden per Role by the
    active Profile when ``profiles`` is given, and held to ``caps``."""
    from trader.llm.registry import build_llm_registry
    from trader.llm.usage import UsageMeter
    from trader.store.repos import UsageRepo

    return build_llm_registry(
        settings,
        UsageMeter(UsageRepo(db)),
        xai_manager=xai_manager,
        profiles=profiles,
        caps=caps,
    )


def build_evaluator(db: Database, settings: Settings, desk=None, *, llm=None, xai_manager=None):
    """The replay eval harness (offline; Nemotron decide role by default — free).
    None without market-data credentials — every builder here must degrade gracefully,
    because ``app = create_app()`` runs at import time (uvicorn's entrypoint). The desk
    is wired in every build so desk-on replays work while the live desk stays dark."""
    from trader.data.composite import build_market_data
    from trader.eval.replay import ReplayHarness

    try:
        provider = build_market_data(settings)
    except ValueError:  # no Alpaca keys in this environment (e.g. CI)
        return None
    return ReplayHarness(
        db,
        llm or build_llm(db, settings, xai_manager=xai_manager),
        provider,
        desk=desk,
        exit_review_enabled=settings.exit_review_enabled,
    )


def build_providers(db: Database, settings: Settings):
    """The model picker's Provider store (#103). Keys are encrypted with TRADER_SECRETS_KEY;
    unset, keyless local Providers still work and keyed ones are refused with a hint."""
    from trader.llm.providers import ProviderDesk

    return ProviderDesk(db, secrets_key=settings.secrets_key)


def build_caps(db: Database, settings: Settings):
    """Per-Role daily Spend caps (#108). The critic's default is its old desk budget."""
    from trader.llm.caps import SpendCaps

    return SpendCaps(db, defaults={"critic": settings.desk_critic_daily_usd})


def build_profiles(db: Database, settings: Settings, providers, caps=None):
    """The model picker's Profiles (#107): the active one serves every Role it names."""
    from trader.llm.profiles import ProfileDesk

    return ProfileDesk(
        db,
        providers,
        nemotron_base_url=settings.nemotron_base_url,
        sentiment_handles=sentiment_handles(settings),
        caps=caps,
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


def build_desk(db: Database, settings: Settings, *, llm=None, xai_manager=None):
    """The decision desk — built in every store-backed build (replay needs it); the LIVE
    loop only receives it when TRADER_DESK_ENABLED=true (see create_app)."""
    from trader.desk.desk import Desk

    # Built alone, it still holds the critic to its default Spend cap.
    return Desk(
        llm or build_llm(db, settings, caps=build_caps(db, settings), xai_manager=xai_manager)
    )


def build_mirror(db: Database, settings: Settings):
    """The one-way markdown mirror; TRADER_RESEARCH_MIRROR_DIR="" disables it."""
    from trader.research.mirror import MarkdownMirror

    if not settings.research_mirror_dir:
        return None
    return MarkdownMirror(db, settings.research_mirror_dir)


def build_compiler(db: Database, settings: Settings, mirror=None, *, llm=None, xai_manager=None):
    """The Macro View compiler — present in every store-backed build, loop or not."""
    from trader.research.compiler import MacroViewCompiler

    return MacroViewCompiler(
        db,
        llm or build_llm(db, settings, xai_manager=xai_manager),
        debounce_seconds=settings.research_compile_debounce_seconds,
        on_compiled=mirror.export_quietly if mirror is not None else None,
    )


def build_briefs(db: Database, settings: Settings, *, llm=None, xai_manager=None):
    """The per-candidate brief compiler — present in every store-backed build. The lane
    into the decide prompt is gated separately (TRADER_BRIEFS_ENABLED, see build_runner)."""
    from trader.research.briefs import BriefCompiler

    return BriefCompiler(
        db,
        llm or build_llm(db, settings, xai_manager=xai_manager),
        symbols=[t.strip() for t in settings.loop_symbols.split(",") if t.strip()],
        debounce_seconds=settings.research_compile_debounce_seconds,
    )


def build_runner(
    db: Database, settings: Settings, sentiment=None, desk=None, *, llm=None, xai_manager=None
):
    """Assemble the decide loop from configured providers (opt-in via TRADER_LOOP_ENABLED)."""
    from trader.brokers.alpaca import AlpacaBroker
    from trader.core.symbols import default_registry
    from trader.data.composite import build_market_data
    from trader.loop.confirm import Confirmer
    from trader.loop.executor import Executor
    from trader.loop.runner import LoopRunner
    from trader.loop.scheduler import SignalScheduler, TriggerConfig
    from trader.risk.guard import RiskGuard
    from trader.risk.limits import limits_from_settings

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

    llm = llm or build_llm(db, settings, xai_manager=xai_manager)
    return LoopRunner(
        db=db,
        market_data=build_market_data(settings),
        llm=llm,
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
                max_heartbeats_per_tick=settings.loop_max_heartbeats_per_tick,
                sentiment_enabled=settings.sentiment_trigger_enabled,
                sentiment_swing=settings.sentiment_swing_threshold,
            ),
        ),
        executor=executor,
        sentiment=sentiment,
        sentiment_poll_seconds=settings.sentiment_poll_seconds,
        shadow_symbols={t.strip() for t in settings.shadow_symbols.split(",") if t.strip()},
        briefs_enabled=settings.briefs_enabled,
        exit_review_enabled=settings.exit_review_enabled,
        desk=desk if settings.desk_enabled else None,
        # Always present: with nothing serving the confirm Role (no .env Jev, no Profile
        # Chain, or switched off) it returns no record and the column stays NULL.
        confirm=Confirmer(llm),
    )


def build_xai_http(settings: Settings, *, xai_manager=None):
    """The service's authenticated api.x.ai client for the operator diagnostics
    (`trader xai probe`): the roles' credential path, None without any xAI credential."""
    from trader.llm.xai import xai_http_client

    return xai_http_client(settings, manager=xai_manager)


app = create_app()
