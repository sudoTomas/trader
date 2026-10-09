"""Tradable universe: asset classes, per-class policy defaults, and the symbol registry.

Pure, no I/O. Entries are generated from per-class defaults plus overrides — config or a
future screener can mint symbols the same way the seeds below do, instead of hand-wiring
every field per symbol (the v1 mistake).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from trader.core.calendars import MarketCalendar, get_calendar


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    US_EQUITY = "us_equity"
    COMMODITY_ETF = "commodity_etf"


@dataclass(frozen=True)
class Symbol:
    """One tradable instrument and its venue/policy facts.

    ``ticker`` is the canonical internal name; ``provider_symbols`` maps adapter name →
    that provider's spelling (e.g. alpaca data wants ``BTC/USD``, alpaca trading wants
    ``BTCUSD``).
    """

    ticker: str
    asset_class: AssetClass
    calendar: str  # calendar registry name, resolved via .market_calendar
    fractionable: bool
    shortable: bool
    min_notional: float  # smallest order the venue accepts, in USD
    provider_symbols: dict[str, str] = field(default_factory=dict)
    # The venue's word on whether it trades this symbol at all; refreshed from Alpaca
    # when the loop starts (#73). A symbol it will not trade takes no opens.
    tradable: bool = True

    def for_provider(self, provider: str) -> str:
        """This symbol as ``provider`` spells it (falls back to the canonical ticker)."""
        return self.provider_symbols.get(provider, self.ticker)

    @property
    def market_calendar(self) -> MarketCalendar:
        return get_calendar(self.calendar)


#: Per-class policy defaults; a symbol is (class defaults + overrides).
_CLASS_DEFAULTS: dict[AssetClass, dict] = {
    AssetClass.CRYPTO: {
        "calendar": "always_open",
        "fractionable": True,
        "shortable": False,  # Alpaca spot crypto is long-only
        "min_notional": 1.0,
    },
    AssetClass.US_EQUITY: {
        "calendar": "nyse",
        "fractionable": True,
        "shortable": True,
        "min_notional": 1.0,
    },
    AssetClass.COMMODITY_ETF: {
        "calendar": "nyse",
        "fractionable": True,
        "shortable": False,  # exposure vehicle, not a short candidate
        "min_notional": 1.0,
    },
}


def make_symbol(ticker: str, asset_class: AssetClass, **overrides) -> Symbol:
    """Mint a Symbol from its class defaults plus explicit overrides."""
    fields = {**_CLASS_DEFAULTS[asset_class], **overrides}
    return Symbol(ticker=ticker, asset_class=asset_class, **fields)


class SymbolRegistry:
    """Lookup by ticker; unknown symbols raise so money-path callers fail safe."""

    def __init__(self, symbols: list[Symbol]) -> None:
        self._by_ticker = {s.ticker: s for s in symbols}
        if len(self._by_ticker) != len(symbols):
            raise ValueError("duplicate ticker in symbol universe")

    def get(self, ticker: str) -> Symbol:
        try:
            return self._by_ticker[ticker]
        except KeyError:
            raise KeyError(
                f"unknown symbol '{ticker}' (known: {sorted(self._by_ticker)})"
            ) from None

    def by_class(self, asset_class: AssetClass) -> list[Symbol]:
        return [s for s in self._by_ticker.values() if s.asset_class == asset_class]

    def all(self) -> list[Symbol]:
        return list(self._by_ticker.values())

    def with_symbol(self, symbol: Symbol) -> SymbolRegistry:
        """A new registry with ``symbol`` added/replaced (registries stay immutable)."""
        return SymbolRegistry([*[s for s in self.all() if s.ticker != symbol.ticker], symbol])


def default_registry() -> SymbolRegistry:
    """The seed universe: the crypto slice (M1), the equities slice (SPY), and the
    commodity ETF slice (GLD/SLV/USO)."""
    return SymbolRegistry(
        [
            make_symbol(
                "BTC/USD",
                AssetClass.CRYPTO,
                provider_symbols={"alpaca_data": "BTC/USD", "alpaca_trading": "BTCUSD"},
            ),
            make_symbol(
                "ETH/USD",
                AssetClass.CRYPTO,
                provider_symbols={"alpaca_data": "ETH/USD", "alpaca_trading": "ETHUSD"},
            ),
            # Long-only FIRST despite the venue allowing shorts — shortability is its
            # own later gate, not a day-one equities feature
            # (see docs/lessons.md#per-symbol-shadow).
            make_symbol("SPY", AssetClass.US_EQUITY, shortable=False),
            # Commodity ETFs (#4): exposure vehicles, long-only by class default, on the
            # NYSE calendar. Alpaca spells ETFs like equities on both the data and the
            # trading API, so the canonical ticker is the provider spelling. A new
            # ticker shadows via TRADER_SHADOW_SYMBOLS before it may submit
            # (see docs/lessons.md#per-symbol-shadow).
            make_symbol("GLD", AssetClass.COMMODITY_ETF),
            make_symbol("SLV", AssetClass.COMMODITY_ETF),
            make_symbol("USO", AssetClass.COMMODITY_ETF),
        ]
    )


__all__ = ["AssetClass", "Symbol", "SymbolRegistry", "default_registry", "make_symbol"]
