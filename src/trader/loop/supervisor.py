"""Supervisor (#126) — N Bots on the one account. Plain async, no LangGraph.

The Supervisor owns what the Bots share and nothing else:

- **Market data, once per tick.** ``SharedMarketData`` fetches each symbol's quote and
  bars once per tick however many Bots read it, and keeps them in memory for that tick
  only. Nothing is stored (see docs/lessons.md#no-market-data-storage).
- **The decide model.** One ``asyncio.Semaphore`` across every Bot: at most
  ``TRADER_LOOP_MAX_CONCURRENT_DECIDES`` (default 2) decide calls at once, so a second
  Bot cannot double the load on a self-hosted model.
- **The one executor and its one fills stream** (see docs/lessons.md#fills-stream-must-be-owned).
  A Bot with no live symbol gets no executor at all.

Everything else is per Bot, in its ``BotRunner``: triggers and cooldowns, journal
history, the single-flight lock, its pause (its own risk_state row), its Sleeve, its
Sources and its prompt. A tick runs the due Bots with ``asyncio.gather``; one Bot's crash
or transient backoff holds back that Bot only.

Bot #1 is built from Settings (``surfaces/api/app.py`` build_runner) and is always first.
Every other Bot comes from the store: each tick the Supervisor reads the Bots' current
versions and (re)builds the runner of any Bot whose version moved, so a new Bot or a new
version runs from the next tick without a restart.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trader.core.symbols import Symbol, SymbolRegistry
from trader.loop.bots import BOT_ONE, LIVE, SHADOW, BotNotFound, triggers_from_spec
from trader.loop.runner import BotRunner, WakeupResult
from trader.loop.scheduler import SignalScheduler

log = structlog.get_logger(__name__)

#: A cached fetch older than this is refetched even inside one tick (a tick that ran
#: long behind a slow model call); well under the 60 s default tick.
SHARED_MAX_AGE_SECONDS = 30.0


class SharedMarketData:
    """A market-data provider read through a per-tick, in-memory cache.

    Concurrent readers of the same quote or bars share one in-flight fetch; a failed
    fetch is shared too, so an outage costs one call per symbol per tick, not one per
    Bot. ``begin_tick`` drops everything. Other attributes (``fred``, ``name``) are the
    provider's.
    """

    def __init__(
        self,
        provider: Any,
        *,
        max_age_seconds: float = SHARED_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self._max_age = max_age_seconds
        self._clock = clock
        self._cache: dict[tuple[Any, ...], tuple[float, asyncio.Future[Any]]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    def begin_tick(self) -> None:
        self._cache.clear()

    async def get_latest_quote(self, symbol: Symbol) -> Any:
        return await self._once(
            ("quote", symbol.ticker), lambda: self.provider.get_latest_quote(symbol)
        )

    async def get_bars(self, symbol: Symbol, interval: str, lookback: int) -> Any:
        return await self._once(
            ("bars", symbol.ticker, interval, lookback),
            lambda: self.provider.get_bars(symbol, interval, lookback),
        )

    async def _once(self, key: tuple[Any, ...], fetch: Callable[[], Awaitable[Any]]) -> Any:
        now = self._clock()
        hit = self._cache.get(key)
        if hit is None or now - hit[0] >= self._max_age:
            task = asyncio.ensure_future(fetch())
            # A failure nobody awaits again must not warn as "never retrieved".
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            hit = self._cache[key] = (now, task)
        # Shielded: one reader's cancellation must not cancel the fetch the others share.
        return await asyncio.shield(hit[1])


def runner_for(
    bot: Mapping[str, Any],
    *,
    template: BotRunner,
    universe: SymbolRegistry,
    desk: Any | None = None,
) -> BotRunner | None:
    """A store-defined Bot's runner, from its current version and the shared pieces of
    ``template`` (Bot #1's runner: store, market data, model, RiskGuard, broker).

    Symbols in replay are not run. None when nothing is left to run. The executor is
    attached only when the build may submit and the Bot is live on a symbol: a Bot
    whose symbols are all shadow has no way to reach the venue.
    """
    spec = bot["spec"]
    modes = {t: m for t, m in spec["symbols"].items() if m in (SHADOW, LIVE)}
    if not modes:
        return None
    symbols = [universe.get(t) for t in modes]
    live = {t for t, m in modes.items() if m == LIVE}
    executor = template.executor if live and not template.shadow else None
    return BotRunner(
        db=template.db,
        market_data=template.market_data,
        llm=template.llm,
        guard=template.guard,
        symbols=symbols,
        shadow=template.shadow,
        tick_seconds=template.tick_seconds,
        equity=template.equity,
        broker=template.broker,  # equity reads only; orders go through the executor
        scheduler=SignalScheduler(symbols, triggers_from_spec(spec["triggers"])),
        executor=executor,
        sentiment=template.sentiment,
        sentiment_poll_seconds=template.sentiment_poll_seconds,
        shadow_symbols=set(modes) - live,
        desk=desk if (spec.get("lanes") or {}).get("desk") else None,
        confirm=template.confirm,
        bot_id=bot["id"],
        bot_version=bot["version"],
        sources=spec["sources"],
        prompt_variant=spec["prompt"]["variant"],
        observes_account=False,
        name=bot["name"],
    )


@dataclass
class Supervisor:
    """Runs every Bot, Bot #1 first. Quacks like the one runner the surfaces knew
    (``guard``, ``executor``, ``equity``, ``stats``, ``symbol``, ``tick``) by speaking
    for Bot #1, so the operator doors keep working unchanged."""

    bots: list[BotRunner]
    shared: SharedMarketData
    tick_seconds: float = 60.0
    max_concurrent_decides: int = 2
    # Builds a store-defined Bot's runner from its current version (``runner_for``
    # bound to this build); None = run only the Bots given.
    factory: Callable[[Mapping[str, Any]], BotRunner | None] | None = None
    _resume_at: dict[int, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_concurrent_decides < 1:
            raise ValueError("max_concurrent_decides must be at least 1")
        self.decide_slots = asyncio.Semaphore(self.max_concurrent_decides)
        for bot in self.bots:
            self._adopt(bot)
        self._task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def _adopt(self, bot: BotRunner) -> None:
        if bot.market_data is not self.shared:
            raise ValueError(f"bot {bot.name!r} must read the Supervisor's shared market data")
        bot.decide_slots = self.decide_slots

    # -- the one runner the surfaces knew, spoken for by Bot #1 ------------------------

    @property
    def lead(self) -> BotRunner:
        return self.bots[0]

    @property
    def executor(self) -> Any | None:
        return self.lead.executor

    @property
    def guard(self) -> Any:
        return self.lead.guard

    @property
    def market_data(self) -> Any:
        """The provider itself: an operator door (an approval's redeem) wants a fresh
        quote, not this tick's."""
        return self.shared.provider

    @property
    def equity(self) -> float:
        return self.lead.equity

    @property
    def shadow(self) -> bool:
        return self.lead.shadow

    @property
    def shadow_symbols(self) -> set[str]:
        return self.lead.shadow_symbols

    @property
    def stats(self) -> Any:
        return self.lead.stats

    def symbol(self, ticker: str) -> Symbol | None:
        """A symbol with its venue flags, from whichever Bot runs it."""
        return next((s for b in self.bots if (s := b.symbol(ticker)) is not None), None)

    def bot(self, name: str | None = None) -> BotRunner:
        """A Bot's runner by name; None is Bot #1."""
        if name is None or name == BOT_ONE:
            return self.lead
        for bot in self.bots:
            if bot.name == name:
                return bot
        raise BotNotFound(name)

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            for bot in self.bots:
                await bot.refresh_venue_flags()
            await self.sync()
            self._stopping.clear()
            self._task = asyncio.create_task(self.run_forever(), name="trader-supervisor")
            log.info("supervisor_started", bots=[b.name for b in self.bots])
        if self.executor is not None and self._stream_task is None:
            # One fills stream for the one account, whichever Bot's order filled.
            self._stream_task = asyncio.create_task(
                self.executor.run_stream(), name="trader-fills-stream"
            )
            log.info("fills_stream_started")

    async def stop(self) -> None:
        self._stopping.set()
        for attr in ("_task", "_stream_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        log.info("supervisor_stopped")

    async def sync(self) -> None:
        """Run each store-defined Bot at its current version: build a new Bot's runner,
        rebuild one whose version moved, drop one with nothing left to run. A Bot whose
        runner cannot be built is logged and left out: it does not run (fail closed)."""
        if self.factory is None:
            return
        rows = await self.lead.db.fetch_all(
            """
            SELECT b.id, b.name, b.current_version AS version, v.spec
            FROM bots b JOIN bot_versions v
              ON v.bot_id = b.id AND v.version = b.current_version
            WHERE b.name <> %s
            ORDER BY b.id
            """,
            (BOT_ONE,),
        )
        running = {b.bot_id: b for b in self.bots[1:]}
        kept: list[BotRunner] = []
        for row in rows:
            current = running.get(row["id"])
            if current is not None and current.bot_version == row["version"]:
                kept.append(current)
                continue
            try:
                built = self.factory(row)
            except Exception as exc:  # noqa: BLE001 - one bad spec must not stop the rest
                log.error("bot_unrunnable", bot=row["name"], error=str(exc))
                continue
            if built is None:
                continue
            self._adopt(built)
            await built.refresh_venue_flags()
            kept.append(built)
            log.info("bot_loaded", bot=row["name"], version=row["version"])
        self.bots[1:] = kept

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            now = datetime.now(tz=UTC)
            try:
                await self.sync()
            except Exception:  # noqa: BLE001 - keep running the Bots already loaded
                log.exception("supervisor_sync_failed")
            self.shared.begin_tick()
            due = [b for b in self.bots if self._resume_at.get(id(b), now) <= now]
            backoffs = await asyncio.gather(*(b.guarded_tick(now) for b in due))
            for bot, backoff in zip(due, backoffs, strict=True):
                if backoff > self.tick_seconds:
                    self._resume_at[id(bot)] = now + timedelta(seconds=backoff)
                else:
                    self._resume_at.pop(id(bot), None)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass

    # -- one pass ----------------------------------------------------------------------

    async def tick(self, now: datetime | None = None, *, force: bool = False) -> list[WakeupResult]:
        """Every Bot's pass, concurrently, over one tick of shared market data."""
        self.shared.begin_tick()
        now = now or datetime.now(tz=UTC)
        passes = await asyncio.gather(*(b.tick(now, force=force) for b in self.bots))
        return [result for results in passes for result in results]

    async def wake(self, name: str | None = None) -> list[WakeupResult]:
        """The manual ``wake_trader`` path for one Bot (None = Bot #1)."""
        bot = self.bot(name)
        self.shared.begin_tick()
        return await bot.tick(force=True)
