"""LoopRunner — the money path, wired. Plain async, no LangGraph.

    SignalScheduler → ContextBuilder → Decider → (Confirm) → (critic) → RiskGuard → Journal
    → (Executor: M3)

**Shadow mode defaults ON**: the full pipeline runs, every decision and risk verdict is
journaled, and nothing is submitted. Turning it off is a deliberate operator act
(``TRADER_SHADOW_MODE=false``) and still cannot bypass RiskGuard — the executor
only ever receives ``RiskOutcome.executable()``.

Operational discipline:

- **Single-flight.** One wakeup at a time. Triggers that arrive while a run is in flight
  are NOT marked dispatched, so they re-fire next tick rather than being swallowed by a
  cooldown they never used (see docs/lessons.md#cooldowns-start-on-dispatch).
- **Transient vs crash backoff.** An infra blip backs off gently and does not arm the
  crash counter; an unexpected exception backs off exponentially. The loop never dies —
  a dead loop is an unmanaged position book (see docs/lessons.md#transient-vs-crash-backoff).
- **Nothing blocking on the event loop.** All I/O here is async so ``/health`` stays
  responsive under a slow model call — a sync tick once blocked it and autoheal
  restart-looped the container (see docs/lessons.md#sync-tick-blocked-health).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from trader.brokers.base import BrokerError
from trader.core.symbols import AssetClass, Symbol
from trader.core.types import Action, Decision, OrderIntent, Side
from trader.data.base import MarketData, MarketDataError
from trader.llm.base import Completion
from trader.llm.registry import LLMRegistry
from trader.loop.confirm import skipped
from trader.loop.context import (
    BAR_LOOKBACK,
    DEFAULT_INTERVALS,
    ContextBuilder,
    compute_indicators,
    unrealized_pnl_frac,
)
from trader.loop.decider import Decider, DeciderError
from trader.loop.journal import (
    DENIED,
    ERROR,
    EXECUTED,
    PENDING,
    SHADOW,
    SUPPRESSED,
    Journal,
)
from trader.loop.scheduler import HEARTBEAT, Mark, SignalScheduler, Trigger
from trader.risk.guard import AccountState, RiskGuard
from trader.store.db import Database
from trader.store.repos import BriefsRepo, MacroViewsRepo, PositionsRepo, SentimentRepo

log = structlog.get_logger(__name__)

TRANSIENT_BACKOFF_SECONDS = 30.0
MAX_CRASH_BACKOFF_SECONDS = 300.0
# Long enough to collapse a burst of reads inside one tick; short enough that the
# next scheduled tick (default 60s) fetches a fresh snapshot.
EQUITY_CACHE_SECONDS = 15.0
EQUITY_FALLBACK = "equity_fallback"
# Realized vol only changes per bar, so the mark's copy lives for the symbol's heartbeat
# cadence (the scheduler's per-asset-class floor); this is the fallback when a scheduler
# does not expose one.
MARK_VOL_FALLBACK_TTL_SECONDS = 300.0
# The deterministic stop (#74). One crossing can be a bad quote, so it takes two ticks.
STOP_LOSS = "stop_loss"
STOP_CONFIRM_TICKS = 2
UNPROTECTED = "no stop: entry unknown"


async def count_trades_today(db: Database, symbol: Symbol, now: datetime) -> int:
    """Orders already recorded in this symbol's session day.

    Every order row counts, including rejects: a failed submit still consumed an
    attempt, and the cap is there to limit churn. Shared by the wakeup path and the
    approval-redeem door so both see the same daily cap.
    """
    start, end = _session_window(symbol, now)
    row = await db.fetch_one(
        "SELECT count(*) AS n FROM orders WHERE created_at >= %s AND created_at < %s",
        (start, end),
    )
    return int(row["n"]) if row else 0


def _session_window(symbol: Symbol, now: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` covering the venue session day that contains ``now``.

    Crypto sessions are UTC dates; NYSE sessions are America/New_York dates.
    ``session_day`` never crosses local midnight, so midnight-to-midnight is the
    whole daily trade window.
    """
    day = symbol.market_calendar.session_day(now)
    tz = ZoneInfo("America/New_York") if symbol.market_calendar.name == "nyse" else UTC
    start = datetime.combine(day, dt_time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


@dataclass
class LoopStats:
    ticks: int = 0
    wakeups: int = 0
    decisions: int = 0
    errors: int = 0
    consecutive_crashes: int = 0
    last_tick_at: str | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "wakeups": self.wakeups,
            "decisions": self.decisions,
            "errors": self.errors,
            "consecutive_crashes": self.consecutive_crashes,
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
        }


@dataclass
class WakeupResult:
    symbol: str
    triggers: list[str]
    status: str
    decision_id: int | None = None
    action: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "triggers": self.triggers,
            "status": self.status,
            "decision_id": self.decision_id,
            "action": self.action,
            "reason": self.reason,
        }


def served(completion: Completion) -> dict[str, Any]:
    """Journal attribution for a decide: the Profile version that routed it (None on
    ``.env``) and the model that actually answered, fallback included (#107)."""
    return {
        "profile_version": completion.profile_version,
        "served_by": f"{completion.provider}:{completion.model}",
    }


@dataclass
class LoopRunner:
    db: Database
    market_data: MarketData
    llm: LLMRegistry
    guard: RiskGuard
    symbols: list[Symbol]
    shadow: bool = True
    tick_seconds: float = 60.0
    equity: float = (
        100_000.0  # shadow source; also the journaled last resort before any broker read
    )
    broker: Any | None = None  # brokers.base.Broker; absent → equity stays the shadow source
    scheduler: SignalScheduler | None = None
    executor: Any | None = None  # loop.executor.Executor; absent = shadow-only build
    sentiment: Any | None = None  # research.sentiment.SentimentService; absent = no polling
    sentiment_poll_seconds: float = 10_800.0
    # Per-symbol throttle: these symbols journal as shadow — full decide + risk pass,
    # nothing submitted — while the rest of the build trades live-paper, so a new
    # symbol earns its gate without pausing the proven slice
    # (see docs/lessons.md#per-symbol-shadow).
    shadow_symbols: set[str] = field(default_factory=set)
    # Briefs lane, dark by default — briefs compile and store regardless,
    # but only this flag lets them into the decide prompt (a decide-behavior change).
    briefs_enabled: bool = False
    # Exit-review lane (#34), dark by default: a held-position block in the prompt
    # (entry, age, unrealized P&L, latest sentiment, an explicit ask to justify holding).
    # A decide-behavior change; flips only through the decide-change gate.
    exit_review_enabled: bool = False
    # Decision desk: analyst lanes before the decide, critic after it.
    # Absent = no desk. Advisory: a crashing desk degrades to a plain decide, never blocks.
    desk: Any | None = None
    # Confirm seat, first stage (#99): Kev judges every open or close Draft and the
    # answer is journaled beside it. Journal-only: it never changes the decision.
    # Absent = no Confirm (TRADER_CONFIRM_PROVIDER empty).
    confirm: Any | None = None  # loop.confirm.Confirmer
    # The Bot this loop runs (#123): every decision, order and parked approval it writes
    # names it. Set at boot once the Bot's version is seeded; None journals no Bot.
    bot_id: int | None = None
    bot_version: int | None = None
    stats: LoopStats = field(default_factory=LoopStats)

    def __post_init__(self) -> None:
        self.scheduler = self.scheduler or SignalScheduler(self.symbols)
        self._decider = Decider(self.llm)
        self._journal = Journal(self.db)
        self._positions = PositionsRepo(self.db)
        self._sentiment_repo = SentimentRepo(self.db)
        self._context = ContextBuilder(
            self.market_data,
            macro_views=MacroViewsRepo(self.db),
            briefs=BriefsRepo(self.db) if self.briefs_enabled else None,
            exit_review_enabled=self.exit_review_enabled,
            sentiment=self._sentiment_repo if self.exit_review_enabled else None,
        )
        self._last_sentiment_poll: dict[str, datetime] = {}
        # Symbols whose last-poll stamp was seeded from the journal (see _poll_sentiment).
        self._sentiment_seeded: set[str] = set()
        # ticker -> (fetched at, realized vol). See _mark_vol.
        self._mark_vols: dict[str, tuple[datetime, float | None]] = {}
        self._equity_as_of: datetime | None = None
        self._last_good_equity: float | None = None
        self._equity_fallback_open = False
        # Stop state, per ticker: consecutive ticks past the stop, the last journaled
        # outcome (so a blocked stop retries every tick but journals once), and the
        # positions already journaled as unprotected.
        self._stop_breaches: dict[str, int] = {}
        self._stop_last: dict[str, tuple[str, str]] = {}
        self._unprotected: set[str] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            await self.refresh_venue_flags()
            self._stopping.clear()
            self._task = asyncio.create_task(self.run_forever(), name="trader-loop")
            log.info("loop_started", shadow=self.shadow, symbols=[s.ticker for s in self.symbols])
        if self.executor is not None and self._stream_task is None:
            # The fills feed lives and dies with the loop: without it the ledger
            # silently diverges on the first fill (found 2026-09-11, first live trade).
            self._stream_task = asyncio.create_task(
                self.executor.run_stream(), name="trader-fills-stream"
            )
            log.info("fills_stream_started")

    async def refresh_venue_flags(self) -> None:
        """Take each stock's and ETF's flags from the venue (#73): whether it trades it,
        and whether it fills fractional orders. Crypto is always fractional. A failed
        lookup fails closed to whole shares; it does not halt the symbol."""
        if self.broker is None or not hasattr(self.broker, "get_asset"):
            return
        refreshed = []
        for symbol in self.symbols:
            if symbol.asset_class is AssetClass.CRYPTO:
                refreshed.append(symbol)
                continue
            try:
                asset = await self.broker.get_asset(symbol.for_provider("alpaca_trading"))
            except BrokerError as exc:
                log.warning("venue_flags_unavailable", symbol=symbol.ticker, error=str(exc))
                refreshed.append(replace(symbol, fractionable=False))
                continue
            refreshed.append(
                replace(symbol, tradable=asset.tradable, fractionable=asset.fractionable)
            )
            log.info(
                "venue_flags",
                symbol=symbol.ticker,
                tradable=asset.tradable,
                fractionable=asset.fractionable,
            )
        self.symbols = refreshed

    def symbol(self, ticker: str) -> Symbol | None:
        """This loop's own copy of a symbol, venue flags included."""
        return next((s for s in self.symbols if s.ticker == ticker), None)

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
        log.info("loop_stopped")

    async def run_forever(self) -> None:
        backoff = 0.0
        while not self._stopping.is_set():
            try:
                await self.tick()
                backoff = 0.0
                self.stats.consecutive_crashes = 0
            except asyncio.CancelledError:
                raise
            except DeciderError as exc:
                # Infra blip: back off gently, do NOT arm the crash counter — v1 crashed
                # the trader on these (see docs/lessons.md#transient-vs-crash-backoff).
                backoff = TRANSIENT_BACKOFF_SECONDS if exc.transient else 0.0
                self.stats.last_error = str(exc)
                log.warning("loop_transient_error", error=str(exc), backoff=backoff)
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                self.stats.consecutive_crashes += 1
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                backoff = min(
                    TRANSIENT_BACKOFF_SECONDS * (2 ** (self.stats.consecutive_crashes - 1)),
                    MAX_CRASH_BACKOFF_SECONDS,
                )
                log.exception(
                    "loop_crashed",
                    consecutive=self.stats.consecutive_crashes,
                    backoff=backoff,
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=max(backoff, self.tick_seconds)
                )
            except TimeoutError:
                pass

    # -- one pass ----------------------------------------------------------

    async def tick(self, now: datetime | None = None, *, force: bool = False) -> list[WakeupResult]:
        """Evaluate triggers and run a wakeup per firing symbol.

        ``force`` is the manual ``wake_trader`` path: it dispatches a heartbeat for every
        symbol whose venue is open, regardless of cooldowns.
        """
        if self._lock.locked():
            # Single-flight: skip WITHOUT marking dispatched so triggers re-fire.
            log.info("loop_tick_skipped_in_flight")
            return []

        async with self._lock:
            now = now or datetime.now(tz=UTC)
            self.stats.ticks += 1
            self.stats.last_tick_at = now.isoformat()

            equity = await self._resolve_equity(now)
            # Holds never reach RiskGuard.evaluate, so the session would otherwise
            # stay frozen until the model wants to trade (#24).
            await self._observe_breakers(now, equity)
            rows = await self._positions.all()
            position_rows = {row["symbol"]: row for row in rows}
            positions = {row["symbol"]: float(row["qty"]) for row in rows}
            entries = {
                row["symbol"]: (
                    float(row["avg_entry_price"]) if row["avg_entry_price"] is not None else None
                )
                for row in rows
            }
            await self._poll_sentiment(now, positions)
            marks = await self._marks(now, positions, entries)
            # The stop runs before the model and pre-empts it for the symbols it holds.
            stopping, stop_results = await self._stops(now, marks, positions, entries, equity)
            assert self.scheduler is not None
            if force:
                triggers = [
                    Trigger(HEARTBEAT, s.ticker, {"forced": True})
                    for s in self.symbols
                    if s.market_calendar.is_open(now)
                ]
            else:
                triggers = self.scheduler.evaluate(now, marks)
            # Not marked dispatched, so they fire again once the stop is done.
            triggers = [t for t in triggers if t.symbol not in stopping]
            if not triggers:
                return stop_results

            by_symbol: dict[str, list[Trigger]] = {}
            for trigger in triggers:
                by_symbol.setdefault(trigger.symbol, []).append(trigger)

            results = [
                await self._wakeup(
                    symbol,
                    fired,
                    now,
                    marks,
                    positions,
                    equity,
                    position_row=position_rows.get(symbol.ticker),
                )
                for symbol in self.symbols
                if (fired := by_symbol.get(symbol.ticker))
            ]
            # Cooldowns start only now — after the wakeups actually ran.
            self.scheduler.mark_dispatched(triggers, now)
            self.stats.wakeups += len(results)
            return stop_results + results

    async def _resolve_equity(self, now: datetime) -> float:
        """Equity for this tick. Shadow builds keep ``self.equity``.

        A broker read is cached for ``EQUITY_CACHE_SECONDS``. When Alpaca is down the
        last good snapshot is reused; the shadow constant is used only if no read has
        ever landed, and either fallback is journaled (once per outage, not per tick).
        """
        if self.broker is None:
            return self.equity
        if (
            self._last_good_equity is not None
            and self._equity_as_of is not None
            and (now - self._equity_as_of).total_seconds() < EQUITY_CACHE_SECONDS
        ):
            return self._last_good_equity
        try:
            snapshot = await self.broker.get_account()
        except BrokerError as exc:
            return await self._equity_fallback(exc)
        equity = float(snapshot.equity)
        if not math.isfinite(equity) or equity <= 0:
            return await self._equity_fallback(BrokerError(f"unusable equity {snapshot.equity!r}"))
        self._last_good_equity = equity
        self._equity_as_of = now
        self.equity = equity
        self._equity_fallback_open = False
        return equity

    async def _equity_fallback(self, exc: Exception) -> float:
        if self._last_good_equity is not None:
            used = self._last_good_equity
            source = "last_good"
        else:
            used = self.equity
            source = "shadow_equity_constant"
        if not self._equity_fallback_open:
            await self._record(
                trigger=EQUITY_FALLBACK,
                status=ERROR,
                raw_output=f"{source} equity={used:.2f}: {exc}",
                prompt=f"broker equity unavailable; source={source}; equity={used:.2f}",
            )
            self._equity_fallback_open = True
        log.warning("equity_fallback", source=source, used=used, error=str(exc))
        return used

    async def _record(self, **row: Any) -> int:
        """Journal a row as this loop's Bot and Bot version."""
        return await self._journal.record(bot_id=self.bot_id, bot_version=self.bot_version, **row)

    async def _trades_today(self, symbol: Symbol, now: datetime) -> int:
        return await count_trades_today(self.db, symbol, now)

    async def _observe_breakers(self, now: datetime, equity: float) -> None:
        """Roll the session and trip loss breakers off this tick's equity.

        The guard still observes again when an intent arrives, so a trade sees
        state at least as fresh as this tick. One row per process: use the latest
        session day among venues that are actually open.
        """
        open_days = [
            symbol.market_calendar.session_day(now)
            for symbol in self.symbols
            if symbol.market_calendar.is_open(now)
        ]
        if not open_days:
            return
        limits = self.guard._limits
        await self.guard.breakers.observe(
            equity=equity,
            session_day=max(open_days),
            daily_loss_limit_frac=limits.daily_loss_limit_frac,
            weekly_drawdown_limit_frac=limits.weekly_drawdown_limit_frac,
        )

    async def _poll_sentiment(self, now: datetime, positions: dict[str, float]) -> None:
        """Collect X sentiment for held, open symbols. Never blocks a tick on failure.

        Unheld names are skipped: the scheduler only reads sentiment for positions,
        so polling the rest is pure cost. The daily call cap inside the service
        still bounds however short ``sentiment_poll_seconds`` is set.

        The cadence survives restarts: the first time a symbol is considered, its
        last-poll stamp is seeded from the latest journaled poll (one query, once per
        symbol), so a redeploy does not re-poll every held name on its first tick.
        No journaled row means poll now.
        """
        if self.sentiment is None:
            return
        for symbol in self.symbols:
            if not symbol.market_calendar.is_open(now):
                continue
            if not positions.get(symbol.ticker):
                continue
            last = await self._seeded_last_sentiment_poll(symbol.ticker)
            if last is not None and (now - last).total_seconds() < self.sentiment_poll_seconds:
                continue
            # Stamp before the call: a failing provider must not be hammered every tick.
            self._last_sentiment_poll[symbol.ticker] = now
            try:
                await self.sentiment.get_x_sentiment(symbol)
            except Exception as exc:  # noqa: BLE001 - sentiment is advisory, the loop is not
                log.warning("sentiment_poll_failed", symbol=symbol.ticker, error=str(exc))

    async def _seeded_last_sentiment_poll(self, ticker: str) -> datetime | None:
        """In-memory last-poll stamp, seeded once per symbol from ``sentiment_scores``."""
        if ticker not in self._sentiment_seeded:
            self._sentiment_seeded.add(ticker)
            row = await self._sentiment_repo.latest(ticker)
            if row is not None and ticker not in self._last_sentiment_poll:
                self._last_sentiment_poll[ticker] = row["ts"]
        return self._last_sentiment_poll.get(ticker)

    async def _latest_sentiment(self, ticker: str) -> float | None:
        row = await self._sentiment_repo.latest(ticker)
        return float(row["score"]) if row is not None else None

    def _mark_vol_ttl(self, symbol: Symbol) -> float:
        """How long a mark's realized vol stays fresh: the symbol's heartbeat cadence."""
        cadence = getattr(getattr(self.scheduler, "_config", None), "heartbeat_seconds", None)
        ttl = cadence.get(symbol.asset_class) if isinstance(cadence, dict) else None
        return float(ttl) if ttl else MARK_VOL_FALLBACK_TTL_SECONDS

    async def _mark_vol(self, symbol: Symbol, now: datetime) -> float | None:
        """Per-bar realized vol on the same interval the decide context uses.

        Cached per symbol for the heartbeat cadence: the value moves once a bar, and
        fetching 100 bars per symbol on every 60 s tick was two Alpaca calls a minute
        for nothing. A bars outage keeps the last known vol (with a warning) so the
        scheduler's baseline does not jump; a symbol with no vol yet stays unset and
        simply cannot see a spike. The quote mark lands either way. The failed
        fetch is not stamped, so the first tick after the outage refreshes.
        """
        cached = self._mark_vols.get(symbol.ticker)
        if cached is not None and (now - cached[0]).total_seconds() < self._mark_vol_ttl(symbol):
            return cached[1]
        try:
            fetched = await self.market_data.get_bars(symbol, DEFAULT_INTERVALS[0], BAR_LOOKBACK)
        except MarketDataError as exc:
            last = cached[1] if cached is not None else None
            log.warning("mark_vol_unavailable", symbol=symbol.ticker, error=str(exc), last_vol=last)
            return last
        vol = compute_indicators(fetched).realized_vol_20
        self._mark_vols[symbol.ticker] = (now, vol)
        return vol

    async def _marks(
        self,
        now: datetime,
        positions: dict[str, float],
        entries: dict[str, float | None],
    ) -> dict[str, Mark]:
        """One cheap quote per symbol, for trigger evaluation.

        A failed quote still yields a zero-price mark rather than dropping the symbol: the
        heartbeat then fires, the wakeup journals as ``suppressed``, and a data outage is
        *visible* in the journal. Dropping the symbol would make an outage indistinguishable
        from a quiet market.
        """
        marks: dict[str, Mark] = {}
        for symbol in self.symbols:
            if not symbol.market_calendar.is_open(now):
                continue
            # Sentiment reaches the scheduler only for HELD symbols: the swing trigger
            # exists to re-examine an open position, not to chase the feed
            # (see docs/lessons.md#sentiment-held-symbols-only).
            score = None
            qty = positions.get(symbol.ticker, 0.0)
            if qty:
                score = await self._latest_sentiment(symbol.ticker)
            vol = await self._mark_vol(symbol, now)
            try:
                quote = await self.market_data.get_latest_quote(symbol)
                price = quote.mid
            except MarketDataError as exc:
                log.warning("mark_unavailable", symbol=symbol.ticker, error=str(exc))
                price = 0.0
            marks[symbol.ticker] = Mark(
                price=price,
                realized_vol=vol,
                unrealized_pnl_frac=unrealized_pnl_frac(price, qty, entries.get(symbol.ticker)),
                sentiment_score=score,
            )
        return marks

    async def _stops(
        self,
        now: datetime,
        marks: dict[str, Mark],
        positions: dict[str, float],
        entries: dict[str, float | None],
        equity: float,
    ) -> tuple[set[str], list[WakeupResult]]:
        """The deterministic stop (#74): no model, just the loss against the entry.

        Returns the tickers whose stop is confirmed this tick (their decide is skipped)
        and a result for every row journaled. A blocked stop (pause, drift, closed venue)
        retries every tick but journals only when its outcome changes; while an order for
        the symbol is still open, nothing is resent.
        """
        frac = self.guard.limits.stop_loss_frac
        if frac <= 0:
            return set(), []
        stopping: set[str] = set()
        results: list[WakeupResult] = []
        for symbol in self.symbols:
            ticker = symbol.ticker
            qty = positions.get(ticker, 0.0)
            if not qty or entries.get(ticker) is None:
                self._stop_breaches.pop(ticker, None)
                self._stop_last.pop(ticker, None)
                if not qty:
                    self._unprotected.discard(ticker)
                elif ticker not in self._unprotected:
                    self._unprotected.add(ticker)
                    results.append(await self._journal_unprotected(symbol))
                continue
            self._unprotected.discard(ticker)
            mark = marks.get(ticker)
            loss = mark.unrealized_pnl_frac if mark is not None else None
            if mark is None or loss is None:
                continue  # venue closed or no quote: no evidence either way
            if loss > -frac:
                self._stop_breaches.pop(ticker, None)
                self._stop_last.pop(ticker, None)
                continue
            ticks = self._stop_breaches[ticker] = self._stop_breaches.get(ticker, 0) + 1
            if ticks < STOP_CONFIRM_TICKS:
                continue
            stopping.add(ticker)
            if await self._order_in_flight(ticker):
                continue
            result = await self._stop(symbol, qty, mark, loss, ticks, now, positions, equity)
            if result is not None:
                results.append(result)
        return stopping, results

    async def _stop(
        self,
        symbol: Symbol,
        qty: float,
        mark: Mark,
        loss: float,
        ticks: int,
        now: datetime,
        positions: dict[str, float],
        equity: float,
    ) -> WakeupResult | None:
        """Close the whole position, by qty, through the same RiskGuard chain."""
        ticker = symbol.ticker
        frac = self.guard.limits.stop_loss_frac
        intent = OrderIntent(ticker, Side.SELL if qty > 0 else Side.BUY, qty=abs(qty))
        decision = Decision(
            ticker,
            Action.CLOSE,
            1.0,
            f"stop-loss: {loss:+.1%} against entry is past -{frac:.1%} on {ticks} ticks",
            intent,
        )
        outcome = await self.guard.evaluate(
            intent,
            symbol,
            price=mark.price,
            account=AccountState(
                equity=equity,
                positions=positions,
                trades_today=await self._trades_today(symbol, now),
            ),
            now=now,
            realized_vol=mark.realized_vol,
            shadow=self.shadow,
            bot_id=self.bot_id,
        )
        record = {
            "trigger": STOP_LOSS,
            "symbol": ticker,
            "asset_class": symbol.asset_class,
            "decision": decision,
            "risk_verdicts": outcome.verdict_dicts,
        }
        if outcome.approved and not (
            self.shadow or self.executor is None or ticker in self.shadow_symbols
        ):
            decision_id = await self._record(status=PENDING, **record)
            execution = await self.executor.execute(
                outcome, symbol, price=mark.price, decision_id=decision_id, bot_id=self.bot_id
            )
            status = EXECUTED if execution.submitted else ERROR
            reason = execution.reason or outcome.reason
            await self._journal.set_status(decision_id, status)
        else:
            status = DENIED if not outcome.approved else SHADOW
            reason = outcome.reason
            if self._stop_last.get(ticker) == (status, reason):
                return None
            decision_id = await self._record(status=status, **record)
        self._stop_last[ticker] = (status, reason)
        log.warning("stop_loss", symbol=ticker, loss=loss, status=status, reason=reason)
        return WakeupResult(
            ticker, [STOP_LOSS], status, decision_id, action=Action.CLOSE, reason=reason
        )

    async def _journal_unprotected(self, symbol: Symbol) -> WakeupResult:
        decision_id = await self._record(
            trigger=STOP_LOSS,
            status=SUPPRESSED,
            symbol=symbol.ticker,
            asset_class=symbol.asset_class,
            raw_output=UNPROTECTED,
        )
        log.warning("stop_loss_unprotected", symbol=symbol.ticker)
        return WakeupResult(symbol.ticker, [STOP_LOSS], SUPPRESSED, decision_id, reason=UNPROTECTED)

    async def _order_in_flight(self, ticker: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT 1 AS open FROM orders WHERE symbol = %s"
            " AND status IN ('new', 'submitted', 'partially_filled') LIMIT 1",
            (ticker,),
        )
        return row is not None

    async def _wakeup(
        self,
        symbol: Symbol,
        triggers: list[Trigger],
        now: datetime,
        marks: dict[str, Mark],
        positions: dict[str, float],
        equity: float,
        *,
        position_row: dict[str, Any] | None = None,  # the exit-review lane's input (#34)
    ) -> WakeupResult:
        names = [t.type for t in triggers]
        trigger_label = ",".join(names)
        started = time.perf_counter()

        # This symbol's own history only: another symbol's holds are not evidence about
        # this one. Equity-fallback rows are an outage audit, not decide context.
        recent = [
            row
            for row in await self._journal.recent(8, symbol=symbol.ticker)
            if row.get("trigger") != EQUITY_FALLBACK
        ][:5]
        ctx = await self._context.build(
            symbol,
            now=now,
            triggers=triggers,
            position_qty=positions.get(symbol.ticker, 0.0),
            equity=equity,
            recent_decisions=recent,
            position=position_row,
        )
        macro_view_version = ctx.macro_view.version if ctx.macro_view else None
        brief_version = ctx.brief.version if ctx.brief else None
        if ctx.price <= 0:
            # No usable market data: journal the wakeup, decide nothing.
            decision_id = await self._record(
                trigger=trigger_label,
                status=SUPPRESSED,
                symbol=symbol.ticker,
                asset_class=symbol.asset_class,
                prompt=ctx.to_prompt(),
                macro_view_version=macro_view_version,
                brief_version=brief_version,
            )
            return WakeupResult(
                symbol.ticker, names, SUPPRESSED, decision_id, reason="no usable price"
            )

        desk_verdicts: dict[str, Any] | None = None
        if self.desk is not None:
            try:
                ctx, desk_verdicts = await self.desk.enrich(ctx)
            except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
                log.warning("desk_enrich_failed", symbol=symbol.ticker, error=str(exc))

        try:
            result = await self._decider.decide(ctx)
        except DeciderError as exc:
            decision_id = await self._record(
                trigger=trigger_label,
                status=ERROR,
                symbol=symbol.ticker,
                asset_class=symbol.asset_class,
                prompt=ctx.to_prompt(),
                raw_output=str(exc),
                desk_verdicts=desk_verdicts,
                macro_view_version=macro_view_version,
                brief_version=brief_version,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            self.stats.errors += 1
            if exc.transient:
                raise  # let run_forever apply transient backoff
            return WakeupResult(symbol.ticker, names, ERROR, decision_id, reason=str(exc))

        decision = result.decision
        confirm: dict[str, Any] | None = None
        if self.confirm is not None and decision.intent is not None:
            try:
                confirm = await self.confirm.confirm(ctx, decision)
            except Exception as exc:  # noqa: BLE001 - Kev must never stop the loop
                log.warning("confirm_failed", symbol=symbol.ticker, error=str(exc)[:300])
                confirm = skipped(decision, f"error ({type(exc).__name__})")
        if self.desk is not None:
            try:
                decision, desk_verdicts = await self.desk.review(ctx, decision, desk_verdicts or {})
            except Exception as exc:  # noqa: BLE001 - a broken critic is a pass-through
                log.warning("desk_review_failed", symbol=symbol.ticker, error=str(exc))
        verdicts: list[dict[str, Any]] | None = None
        status = SHADOW
        reason = ""

        if decision.intent is not None:
            outcome = await self.guard.evaluate(
                decision.intent,
                symbol,
                price=ctx.price,
                account=AccountState(
                    equity=equity,
                    positions=positions,
                    trades_today=await self._trades_today(symbol, now),
                ),
                now=now,
                realized_vol=ctx.primary_vol,
                shadow=self.shadow,
                bot_id=self.bot_id,
            )
            verdicts = outcome.verdict_dicts
            reason = outcome.reason
            if not outcome.approved:
                status = DENIED
            elif self.shadow or self.executor is None or symbol.ticker in self.shadow_symbols:
                status = SHADOW
            else:
                # Persist the decision BEFORE submit so orders.decision_id can point
                # at it. The executor still receives only outcome.executable().
                decision_id = await self._record(
                    trigger=trigger_label,
                    status=PENDING,
                    symbol=symbol.ticker,
                    asset_class=symbol.asset_class,
                    prompt=result.prompt,
                    raw_output=result.raw_output,
                    decision=decision,
                    risk_verdicts=verdicts,
                    desk_verdicts=desk_verdicts,
                    confirm=confirm,
                    macro_view_version=macro_view_version,
                    brief_version=brief_version,
                    cost_usd=result.completion.cost_usd,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    **served(result.completion),
                )
                execution = await self.executor.execute(
                    outcome,
                    symbol,
                    price=ctx.price,
                    decision_id=decision_id,
                    bot_id=self.bot_id,
                )
                status = EXECUTED if execution.submitted else ERROR
                reason = execution.reason or reason
                await self._journal.set_status(decision_id, status)
                self.stats.decisions += 1
                return WakeupResult(
                    symbol.ticker,
                    names,
                    status,
                    decision_id,
                    action=decision.action,
                    reason=reason,
                )

        decision_id = await self._record(
            trigger=trigger_label,
            status=status,
            symbol=symbol.ticker,
            asset_class=symbol.asset_class,
            prompt=result.prompt,
            raw_output=result.raw_output,
            decision=decision,
            risk_verdicts=verdicts,
            desk_verdicts=desk_verdicts,
            confirm=confirm,
            macro_view_version=macro_view_version,
            brief_version=brief_version,
            cost_usd=result.completion.cost_usd,
            latency_ms=int((time.perf_counter() - started) * 1000),
            **served(result.completion),
        )
        self.stats.decisions += 1
        return WakeupResult(
            symbol.ticker, names, status, decision_id, action=decision.action, reason=reason
        )
