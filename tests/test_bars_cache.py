"""Bars cache tests — read-through behavior against the real compose Postgres, plus the
live-path/store separation invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import trader.data.alpaca
import trader.data.base
import trader.data.composite
import trader.data.fred
import trader.data.indicators
import trader.data.twelve_data
from trader.core.symbols import default_registry
from trader.core.types import Bar
from trader.data.bars_cache import STATS, BarsCache, reset_stats
from trader.store.db import Database

BTC = default_registry().get("BTC/USD")


class _CountingProvider:
    name = "stub"

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.calls = 0

    async def get_bars(self, symbol, interval, lookback):
        self.calls += 1
        return self._bars

    async def get_latest_quote(self, symbol):
        raise NotImplementedError


def _window() -> tuple[datetime, datetime, list[Bar]]:
    start = datetime.now(tz=UTC).replace(second=0, microsecond=0) - timedelta(minutes=10)
    end = start + timedelta(minutes=5)
    bars = [
        Bar("BTC/USD", "1m", start + timedelta(minutes=i), 1, 2, 0.5, 1.5, 10.0) for i in range(5)
    ]
    return start, end, bars


async def test_read_through_miss_then_hit(db: Database):
    reset_stats()
    start, end, bars = _window()
    provider = _CountingProvider(bars)
    cache = BarsCache(db, provider)

    first = await cache.ensure_bars(BTC, "1m", start, end)
    assert [b.ts for b in first] == [b.ts for b in bars]
    assert provider.calls == 1 and STATS["misses"] == 1 and STATS["bars_fetched"] == 5

    second = await cache.ensure_bars(BTC, "1m", start, end)
    assert second == first
    assert provider.calls == 1  # served from the table, provider untouched
    assert STATS["hits"] == 1


async def test_prune_bounds_retention(db: Database):
    start, end, bars = _window()
    cache = BarsCache(db, _CountingProvider(bars), retention_days=30)
    await cache.ensure_bars(BTC, "1m", start, end)

    now = datetime.now(tz=UTC)
    assert await cache.prune(now) == 0  # fresh bars stay
    assert await cache.prune(now + timedelta(days=31)) == 5  # past retention → gone


async def test_unsupported_interval_rejected(db: Database):
    cache = BarsCache(db, _CountingProvider([]))
    with pytest.raises(ValueError, match="unsupported interval"):
        await cache.ensure_bars(BTC, "3m", datetime.now(tz=UTC), datetime.now(tz=UTC))


def test_live_path_modules_never_import_the_store():
    """The cache is replay-only: no live-path data module may touch trader.store."""
    live_path_modules = (
        trader.data.base,
        trader.data.alpaca,
        trader.data.twelve_data,
        trader.data.composite,
        trader.data.fred,
        trader.data.indicators,
    )
    for module in live_path_modules:
        source = Path(module.__file__).read_text()
        assert "trader.store" not in source, f"{module.__name__} imports the store"
