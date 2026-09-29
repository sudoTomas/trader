"""Read-through bars cache — REPLAY/SCREENER ONLY, never a live-path dependency.

The live loop calls the provider directly and computes indicators in-process; this module
exists so replay-shaped consumers can ask for a historical window without hammering the
provider. One candle per row, immutable ``(symbol, interval, ts)`` PK, bounded retention —
NOT v1's embedded-window blob that grew to 3.6 GB (see docs/lessons.md#no-market-data-storage).
A test asserts no
live-path data module imports the store.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from trader.core.symbols import Symbol
from trader.core.types import Bar
from trader.data.base import MarketData
from trader.store.db import Database
from trader.store.repos import BarsRepo

log = structlog.get_logger(__name__)

_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}

#: Hard cap on one backfill fetch — keeps a mistaken wide window MB-scale.
_MAX_FETCH_BARS = 10_000

#: Process-wide hit/miss counters, surfaced by the ``cache_stats`` service query.
STATS = {"hits": 0, "misses": 0, "bars_fetched": 0}


def reset_stats() -> None:
    STATS.update({"hits": 0, "misses": 0, "bars_fetched": 0})


class BarsCache:
    """Read-through over the ``bars`` table backed by a provider for misses."""

    def __init__(self, db: Database, provider: MarketData, *, retention_days: int = 90) -> None:
        self._repo = BarsRepo(db)
        self._db = db
        self._provider = provider
        self._retention = timedelta(days=retention_days)

    async def ensure_bars(
        self, symbol: Symbol, interval: str, start: datetime, end: datetime
    ) -> list[Bar]:
        """Cached bars in ``[start, end)``, backfilling from the provider on a miss.

        Coverage test is edge-based (cached rows reach both window edges within one
        interval); gaps inside the window are honest venue gaps, not misses.
        """
        try:
            step = timedelta(seconds=_INTERVAL_SECONDS[interval])
        except KeyError:
            raise ValueError(f"unsupported interval '{interval}'") from None

        rows = await self._repo.range(symbol.ticker, interval, start, end)
        if rows and rows[0]["ts"] <= start + step and rows[-1]["ts"] >= end - step:
            STATS["hits"] += 1
            return [self._to_bar(symbol.ticker, interval, r) for r in rows]

        STATS["misses"] += 1
        # Providers serve "most recent N": reach back far enough to cover start.
        now = datetime.now(tz=start.tzinfo)
        lookback = min(int((now - start) / step) + 1, _MAX_FETCH_BARS)
        fetched = await self._provider.get_bars(symbol, interval, lookback)
        STATS["bars_fetched"] += len(fetched)
        if fetched:
            await self._repo.upsert_many(
                [
                    {
                        "symbol": b.symbol,
                        "interval": b.interval,
                        "ts": b.ts,
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                    }
                    for b in fetched
                ]
            )
        rows = await self._repo.range(symbol.ticker, interval, start, end)
        return [self._to_bar(symbol.ticker, interval, r) for r in rows]

    async def prune(self, now: datetime) -> int:
        """Drop bars past retention. Returns rows deleted (bounded-retention invariant)."""
        deleted = await self._db.execute("DELETE FROM bars WHERE ts < %s", (now - self._retention,))
        if deleted:
            log.info("bars_cache_pruned", deleted=deleted)
        return deleted

    @staticmethod
    def _to_bar(ticker: str, interval: str, row: dict) -> Bar:
        return Bar(
            symbol=ticker,
            interval=interval,
            ts=row["ts"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
