"""Market data contract. Fetch-on-tick: providers return bars/quotes, nothing here
persists anything (the replay-only bars cache is a separate, non-live-path concern).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trader.core.symbols import Symbol
from trader.core.types import Bar, Quote


class MarketDataError(RuntimeError):
    """A provider could not answer. CompositeMarketData catches these to fall through."""


@runtime_checkable
class MarketData(Protocol):
    """One market data provider."""

    name: str

    async def get_bars(self, symbol: Symbol, interval: str, lookback: int) -> list[Bar]:
        """The most recent ``lookback`` candles, ascending (``bars[-1]`` is latest).

        Gap-honest: only bars the venue actually traded — nothing synthetic over
        closed sessions, so per-bar math naturally spans weekends/halts.
        """
        ...

    async def get_latest_quote(self, symbol: Symbol) -> Quote:
        """The latest bid/ask (or last price quoted both sides)."""
        ...
