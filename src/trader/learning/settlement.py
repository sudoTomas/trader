"""Settlement (#143) — every journaled decision scored 5 trading days after it was made.

The outcome is measured from daily bars, never from a model: the symbol's close on the
session the decision was made in (the first daily bar that ends after it) is the entry,
the close ``HORIZON_DAYS`` of the symbol's own sessions later is the exit. The benchmark
(SPY for equities and ETFs, BTC/USD for crypto) is valued at its last close on or before
each of those bars, so both returns span the same dates. A window that has not fully
traded stays pending and is tried again on the next pass.

``settled_at`` is the end of the exit bar: the instant the outcome became known, and so
the instant a Lesson about it may be read (replay never reads one before it).

Bars are fetched on use and dropped; only the outcome is stored (see
docs/lessons.md#no-market-data-storage). Settlement is advisory bookkeeping: it never
touches the money path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trader.core.symbols import AssetClass, Symbol, SymbolRegistry
from trader.core.types import Bar
from trader.data.base import MarketDataError
from trader.llm.base import LLMError
from trader.store.db import Database
from trader.store.repos import LessonsRepo, SettlementsRepo

log = structlog.get_logger(__name__)

#: The owner's horizon (PRD #145, decision 6): five of the symbol's sessions.
HORIZON_DAYS = 5
DAY = timedelta(days=1)
#: Daily bars fetched per symbol per pass; decisions older than the bars reach are
#: skipped (MAX_AGE_DAYS keeps them out of the pending query).
LOOKBACK_BARS = 120
MAX_AGE_DAYS = 90
EQUITY_BENCHMARK = "SPY"
CRYPTO_BENCHMARK = "BTC/USD"


def benchmark_for(symbol: Symbol) -> str:
    return CRYPTO_BENCHMARK if symbol.asset_class is AssetClass.CRYPTO else EQUITY_BENCHMARK


def direction(decision: Mapping[str, Any] | None) -> int:
    """The decision's view: +1 long (an open buy, a short's close), -1 short (an open
    sell, a long's close), 0 a hold."""
    if not decision or decision.get("action") == "hold":
        return 0
    side = (decision.get("intent") or {}).get("side")
    return {"buy": 1, "sell": -1}.get(side, 0)


@dataclass(frozen=True)
class Outcome:
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    benchmark_return: float
    forward_return: float
    settled_at: datetime

    @property
    def excess_return(self) -> float:
        return self.forward_return - self.benchmark_return


def _asof(bars: Sequence[Bar], ts: datetime) -> Bar | None:
    eligible = [b for b in bars if b.ts <= ts]
    return eligible[-1] if eligible else None


def measure(
    decided_at: datetime,
    bars: Sequence[Bar],
    benchmark: Sequence[Bar],
    *,
    now: datetime,
    horizon: int = HORIZON_DAYS,
) -> Outcome | None:
    """The outcome of a decision made at ``decided_at`` from ascending daily bars of the
    symbol and its benchmark; None while the window has not fully traded by ``now``."""
    entry_at = next((i for i, b in enumerate(bars) if b.ts + DAY > decided_at), None)
    if entry_at is None or entry_at + horizon >= len(bars):
        return None
    entry, exit_ = bars[entry_at], bars[entry_at + horizon]
    settled_at = exit_.ts + DAY
    if settled_at > now or entry.close <= 0:
        return None
    bench_entry, bench_exit = _asof(benchmark, entry.ts), _asof(benchmark, exit_.ts)
    # The benchmark must have a close at the entry and have traded through the exit.
    if bench_entry is None or bench_exit is None or bench_entry.close <= 0:
        return None
    if benchmark[-1].ts < exit_.ts:
        return None
    return Outcome(
        entry_ts=entry.ts,
        entry_price=entry.close,
        exit_ts=exit_.ts,
        exit_price=exit_.close,
        benchmark_return=bench_exit.close / bench_entry.close - 1,
        forward_return=exit_.close / entry.close - 1,
        settled_at=settled_at,
    )


def correct(view: int, excess_return: float) -> bool | None:
    """Whether a directional view matched the sign of the excess return; None for a hold."""
    if view == 0:
        return None
    return view * excess_return > 0


class Settler:
    """One settlement pass: settle what has traded, then let the reflect Role write a
    Lesson for up to ``max_reflections`` of the newest settled decisions."""

    def __init__(
        self,
        db: Database,
        market_data: Any,  # MarketData: daily bars, fetched on use
        *,
        symbols: SymbolRegistry,
        reflector: Any | None = None,  # learning.reflect.Reflector; None = no Lessons
        max_reflections: int = 10,
        horizon: int = HORIZON_DAYS,
    ) -> None:
        self._data = market_data
        self._symbols = symbols
        self._reflector = reflector
        self._max_reflections = max_reflections
        self._horizon = horizon
        self._settlements = SettlementsRepo(db)
        self._lessons = LessonsRepo(db)
        self._lock = asyncio.Lock()

    async def settle(self, now: datetime | None = None) -> dict[str, Any]:
        """Settle every pending decision whose window has traded. Never raises for one
        symbol's bars: that symbol stays pending and the pass goes on."""
        now = now or datetime.now(tz=UTC)
        async with self._lock:
            settled, pending, failed = await self._settle(now)
            lessons = await self._reflect()
        out = {"settled": settled, "pending": pending, "failed": failed, "lessons": lessons}
        log.info("settlement_pass", **{k: v for k, v in out.items() if k != "failed"})
        return out

    async def _settle(self, now: datetime) -> tuple[int, int, list[dict[str, str]]]:
        rows = await self._settlements.pending(
            now=now, min_age_days=self._horizon, max_age_days=MAX_AGE_DAYS
        )
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_symbol.setdefault(row["symbol"], []).append(row)
        bars: dict[str, list[Bar]] = {}
        settled = pending = 0
        failed: list[dict[str, str]] = []
        for ticker, decisions in by_symbol.items():
            try:
                symbol = self._symbols.get(ticker)
                bench = self._symbols.get(benchmark_for(symbol))
                for s in (symbol, bench):
                    if s.ticker not in bars:
                        bars[s.ticker] = await self._data.get_bars(s, "1d", LOOKBACK_BARS)
            except (KeyError, MarketDataError) as exc:
                log.warning("settlement_bars_unavailable", symbol=ticker, error=str(exc))
                failed.append({"symbol": ticker, "error": str(exc)[:200]})
                pending += len(decisions)
                continue
            for row in decisions:
                outcome = measure(
                    row["ts"], bars[ticker], bars[bench.ticker], now=now, horizon=self._horizon
                )
                if outcome is None:
                    pending += 1
                    continue
                view = direction(row["decision"])
                await self._settlements.insert(
                    decision_id=row["id"],
                    bot_id=row["bot_id"],
                    symbol=ticker,
                    horizon_days=self._horizon,
                    entry_ts=outcome.entry_ts,
                    entry_price=outcome.entry_price,
                    exit_ts=outcome.exit_ts,
                    exit_price=outcome.exit_price,
                    benchmark=bench.ticker,
                    benchmark_return=outcome.benchmark_return,
                    forward_return=outcome.forward_return,
                    excess_return=outcome.excess_return,
                    direction=view,
                    correct=correct(view, outcome.excess_return),
                    settled_at=outcome.settled_at,
                )
                settled += 1
        return settled, pending, failed

    async def _reflect(self) -> int:
        if self._reflector is None or self._max_reflections <= 0:
            return 0
        written = 0
        for row in await self._settlements.unreflected(self._max_reflections):
            try:
                lesson = await self._reflector.lesson(row)
            except LLMError as exc:
                # The Role is down, or capped with no free model: try again next pass.
                log.warning("reflect_unavailable", error=str(exc)[:300])
                break
            if lesson is None:
                continue
            text, written_by = lesson
            await self._lessons.insert(
                decision_id=row["decision_id"],
                bot_id=row["bot_id"],
                symbol=row["symbol"],
                lesson=text,
                written_by=written_by,
                available_at=row["settled_at"],
            )
            written += 1
        return written

    async def run_forever(self, interval_seconds: float, stopping: asyncio.Event) -> None:
        """A pass every ``interval_seconds`` until ``stopping``; a failed pass is logged."""
        while not stopping.is_set():
            try:
                await self.settle()
            except Exception:  # noqa: BLE001 - bookkeeping must never take the process down
                log.exception("settlement_pass_failed")
            try:
                await asyncio.wait_for(stopping.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
