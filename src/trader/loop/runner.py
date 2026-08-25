"""LoopRunner — the money path, wired. Plain async, no LangGraph.

    SignalScheduler → ContextBuilder → Decider → RiskGuard → Journal → (Executor: M3)

**Shadow mode defaults ON**: the full pipeline runs, every decision and risk verdict is
journaled, and nothing is submitted. Turning it off is a deliberate operator act
(``TRADER_SHADOW_MODE=false``) and still cannot bypass RiskGuard — the executor (EAI-176)
only ever receives ``RiskOutcome.executable()``.

Operational discipline:

- **Single-flight.** One wakeup at a time. Triggers that arrive while a run is in flight
  are NOT marked dispatched, so they re-fire next tick rather than being swallowed by a
  cooldown they never used (v1:engine/signal_monitor.py).
- **Transient vs crash backoff** (v1:EAI-40). An infra blip backs off gently and does not
  arm the crash counter; an unexpected exception backs off exponentially. The loop never
  dies — a dead loop is an unmanaged position book.
- **Nothing blocking on the event loop.** All I/O here is async so ``/health`` stays
  responsive under a slow model call (v1:EAI-39).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from trader.core.symbols import Symbol
from trader.data.base import MarketData, MarketDataError
from trader.llm.registry import LLMRegistry
from trader.loop.context import ContextBuilder
from trader.loop.decider import Decider, DeciderError
from trader.loop.journal import (
    DENIED,
    ERROR,
    EXECUTED,
    SHADOW,
    SUPPRESSED,
    Journal,
)
from trader.loop.scheduler import HEARTBEAT, Mark, SignalScheduler, Trigger
from trader.risk.guard import AccountState, RiskGuard
from trader.store.db import Database
from trader.store.repos import MacroViewsRepo, PositionsRepo, SentimentRepo

log = structlog.get_logger(__name__)

TRANSIENT_BACKOFF_SECONDS = 30.0
MAX_CRASH_BACKOFF_SECONDS = 300.0


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


@dataclass
class LoopRunner:
    db: Database
    market_data: MarketData
    llm: LLMRegistry
    guard: RiskGuard
    symbols: list[Symbol]
    shadow: bool = True
    tick_seconds: float = 60.0
    equity: float = 100_000.0  # fallback when no broker is attached
    scheduler: SignalScheduler | None = None
    executor: Any | None = None  # loop.executor.Executor; absent = shadow-only build
    sentiment: Any | None = None  # research.sentiment.SentimentService; absent = no polling
    sentiment_poll_seconds: float = 3600.0
    stats: LoopStats = field(default_factory=LoopStats)

    def __post_init__(self) -> None:
        self.scheduler = self.scheduler or SignalScheduler(self.symbols)
        self._context = ContextBuilder(self.market_data, macro_views=MacroViewsRepo(self.db))
        self._decider = Decider(self.llm)
        self._journal = Journal(self.db)
        self._positions = PositionsRepo(self.db)
        self._sentiment_repo = SentimentRepo(self.db)
        self._last_sentiment_poll: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self.run_forever(), name="trader-loop")
            log.info("loop_started", shadow=self.shadow, symbols=[s.ticker for s in self.symbols])

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
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
                # Infra blip: back off gently, do NOT arm the crash counter (v1:EAI-40).
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

            positions = {row["symbol"]: float(row["qty"]) for row in await self._positions.all()}
            await self._poll_sentiment(now)
            marks = await self._marks(now, positions)
            assert self.scheduler is not None
            if force:
                triggers = [
                    Trigger(HEARTBEAT, s.ticker, {"forced": True})
                    for s in self.symbols
                    if s.market_calendar.is_open(now)
                ]
            else:
                triggers = self.scheduler.evaluate(now, marks)
            if not triggers:
                return []

            by_symbol: dict[str, list[Trigger]] = {}
            for trigger in triggers:
                by_symbol.setdefault(trigger.symbol, []).append(trigger)

            results = [
                await self._wakeup(symbol, fired, now, marks, positions)
                for symbol in self.symbols
                if (fired := by_symbol.get(symbol.ticker))
            ]
            # Cooldowns start only now — after the wakeups actually ran.
            self.scheduler.mark_dispatched(triggers, now)
            self.stats.wakeups += len(results)
            return results

    async def _poll_sentiment(self, now: datetime) -> None:
        """Collect X sentiment on a cadence (shadow record). Never blocks a tick on failure."""
        if self.sentiment is None:
            return
        for symbol in self.symbols:
            if not symbol.market_calendar.is_open(now):
                continue
            last = self._last_sentiment_poll.get(symbol.ticker)
            if last is not None and (now - last).total_seconds() < self.sentiment_poll_seconds:
                continue
            # Stamp before the call: a failing provider must not be hammered every tick.
            self._last_sentiment_poll[symbol.ticker] = now
            try:
                await self.sentiment.get_x_sentiment(symbol)
            except Exception as exc:  # noqa: BLE001 - sentiment is advisory, the loop is not
                log.warning("sentiment_poll_failed", symbol=symbol.ticker, error=str(exc))

    async def _latest_sentiment(self, ticker: str) -> float | None:
        row = await self._sentiment_repo.latest(ticker)
        return float(row["score"]) if row is not None else None

    async def _marks(self, now: datetime, positions: dict[str, float]) -> dict[str, Mark]:
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
            # Sentiment reaches the scheduler only for HELD symbols (EAI-180): the swing
            # trigger exists to re-examine an open position, not to chase the feed.
            score = None
            if positions.get(symbol.ticker):
                score = await self._latest_sentiment(symbol.ticker)
            try:
                quote = await self.market_data.get_latest_quote(symbol)
                marks[symbol.ticker] = Mark(price=quote.mid, sentiment_score=score)
            except MarketDataError as exc:
                log.warning("mark_unavailable", symbol=symbol.ticker, error=str(exc))
                marks[symbol.ticker] = Mark(price=0.0, sentiment_score=score)
        return marks

    async def _wakeup(
        self,
        symbol: Symbol,
        triggers: list[Trigger],
        now: datetime,
        marks: dict[str, Mark],
        positions: dict[str, float],
    ) -> WakeupResult:
        names = [t.type for t in triggers]
        trigger_label = ",".join(names)
        started = time.perf_counter()

        ctx = await self._context.build(
            symbol,
            now=now,
            triggers=triggers,
            position_qty=positions.get(symbol.ticker, 0.0),
            equity=self.equity,
            recent_decisions=await self._journal.recent(5),
        )
        macro_view_version = ctx.macro_view.version if ctx.macro_view else None
        if ctx.price <= 0:
            # No usable market data: journal the wakeup, decide nothing.
            decision_id = await self._journal.record(
                trigger=trigger_label,
                status=SUPPRESSED,
                symbol=symbol.ticker,
                asset_class=symbol.asset_class,
                prompt=ctx.to_prompt(),
                macro_view_version=macro_view_version,
            )
            return WakeupResult(
                symbol.ticker, names, SUPPRESSED, decision_id, reason="no usable price"
            )

        try:
            result = await self._decider.decide(ctx)
        except DeciderError as exc:
            decision_id = await self._journal.record(
                trigger=trigger_label,
                status=ERROR,
                symbol=symbol.ticker,
                asset_class=symbol.asset_class,
                prompt=ctx.to_prompt(),
                raw_output=str(exc),
                macro_view_version=macro_view_version,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            self.stats.errors += 1
            if exc.transient:
                raise  # let run_forever apply transient backoff
            return WakeupResult(symbol.ticker, names, ERROR, decision_id, reason=str(exc))

        decision = result.decision
        verdicts: list[dict[str, Any]] | None = None
        status = SHADOW
        reason = ""

        if decision.intent is not None:
            outcome = await self.guard.evaluate(
                decision.intent,
                symbol,
                price=ctx.price,
                account=AccountState(
                    equity=self.equity,
                    positions=positions,
                    trades_today=0,  # real count arrives with the executor (EAI-176)
                ),
                now=now,
                realized_vol=ctx.primary_vol,
                shadow=self.shadow,
            )
            verdicts = outcome.verdict_dicts
            reason = outcome.reason
            if not outcome.approved:
                status = DENIED
            elif self.shadow or self.executor is None:
                status = SHADOW
            else:
                # Live-paper: the executor takes ONLY outcome.executable(), which raises
                # unless the chain approved. There is no other path to the broker.
                execution = await self.executor.execute(
                    outcome, symbol, price=ctx.price, decision_id=None
                )
                status = EXECUTED if execution.submitted else ERROR
                reason = execution.reason or reason

        decision_id = await self._journal.record(
            trigger=trigger_label,
            status=status,
            symbol=symbol.ticker,
            asset_class=symbol.asset_class,
            prompt=result.prompt,
            raw_output=result.raw_output,
            decision=decision,
            risk_verdicts=verdicts,
            macro_view_version=macro_view_version,
            cost_usd=result.completion.cost_usd,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        self.stats.decisions += 1
        return WakeupResult(
            symbol.ticker, names, status, decision_id, action=decision.action, reason=reason
        )
