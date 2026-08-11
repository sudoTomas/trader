"""Provider routing: an ordered chain per asset class, degrade with a warning.

A failing primary logs and falls through to the next provider; only a fully exhausted
chain raises ``MarketDataError``. The caller (loop/context builder) decides what a
missed tick means — this layer never crashes the process.
"""

from __future__ import annotations

import structlog

from trader.core.symbols import AssetClass, Symbol
from trader.core.types import Bar, Quote
from trader.data.base import MarketData, MarketDataError
from trader.ops.config import Settings

log = structlog.get_logger(__name__)


class CompositeMarketData:
    """Asset-class-routed provider chains."""

    name = "composite"

    def __init__(self, chains: dict[AssetClass, list[MarketData]]) -> None:
        for asset_class, chain in chains.items():
            if not chain:
                raise ValueError(f"empty provider chain for {asset_class}")
        self._chains = chains

    def _chain(self, symbol: Symbol) -> list[MarketData]:
        try:
            return self._chains[symbol.asset_class]
        except KeyError:
            raise MarketDataError(
                f"no market data providers configured for {symbol.asset_class}"
            ) from None

    async def _try_chain(self, symbol: Symbol, op: str, call):
        errors: list[str] = []
        for provider in self._chain(symbol):
            try:
                return await call(provider)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                log.warning(
                    "market_data_provider_failed",
                    provider=provider.name,
                    op=op,
                    symbol=symbol.ticker,
                    error=str(exc),
                )
        raise MarketDataError(f"all providers failed for {op}({symbol.ticker}): {errors}")

    async def get_bars(self, symbol: Symbol, interval: str, lookback: int) -> list[Bar]:
        return await self._try_chain(
            symbol, "get_bars", lambda p: p.get_bars(symbol, interval, lookback)
        )

    async def get_latest_quote(self, symbol: Symbol) -> Quote:
        return await self._try_chain(
            symbol, "get_latest_quote", lambda p: p.get_latest_quote(symbol)
        )


def build_market_data(settings: Settings) -> CompositeMarketData:
    """Wire the provider chains this build can actually serve, from configured keys."""
    from trader.data.alpaca import AlpacaData
    from trader.data.twelve_data import TwelveData

    alpaca = AlpacaData(settings.alpaca_api_key_id, settings.alpaca_api_secret)
    equities_chain: list[MarketData] = [alpaca]
    if settings.twelve_data_api_key:
        equities_chain.append(TwelveData(settings.twelve_data_api_key))
    return CompositeMarketData(
        {
            AssetClass.CRYPTO: [alpaca],
            AssetClass.US_EQUITY: equities_chain,
            AssetClass.COMMODITY_ETF: equities_chain,
        }
    )
