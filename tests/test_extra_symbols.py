"""Extra symbols — US-listed stocks or ETFs added by setting (TRADER_EXTRA_SYMBOLS).

An extra is minted long-only on the NYSE calendar, the seed universe always wins for a
ticker it already defines, and a malformed ticker raises instead of reaching the money
path. Being known is not being watched: the loop still only sees TRADER_LOOP_SYMBOLS,
and a new name earns its flip in TRADER_SHADOW_SYMBOLS first
(see docs/lessons.md#per-symbol-shadow).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trader.core.symbols import AssetClass, default_registry
from trader.ops.config import Settings

OPEN = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)  # Wednesday 11:00 ET, NYSE open
CLOSED = datetime(2026, 8, 12, 21, 0, tzinfo=UTC)  # Wednesday 17:00 ET


def test_no_extras_keeps_the_seed_universe_unchanged():
    seed = {s.ticker for s in default_registry().all()}
    assert seed == {"BTC/USD", "ETH/USD", "SPY", "GLD", "SLV", "USO"}
    assert {s.ticker for s in default_registry(()).all()} == seed


def test_extras_are_minted_long_only_us_equities_on_the_nyse_calendar():
    registry = default_registry(["PLTR", "AMZN"])
    for ticker in ("PLTR", "AMZN"):
        sym = registry.get(ticker)
        assert sym.asset_class is AssetClass.US_EQUITY
        assert sym.shortable is False
        assert sym.market_calendar.is_open(OPEN)
        assert not sym.market_calendar.is_open(CLOSED)
        assert sym.for_provider("alpaca_trading") == ticker


def test_extras_are_normalized_and_deduplicated():
    registry = default_registry([" pltr ", "PLTR", "", "brk.b"])
    tickers = [s.ticker for s in registry.all()]
    assert tickers.count("PLTR") == 1
    assert "BRK.B" in tickers


def test_a_seed_ticker_listed_as_extra_keeps_its_seed_definition():
    registry = default_registry(["GLD", "SPY"])
    assert registry.get("GLD").asset_class is AssetClass.COMMODITY_ETF
    assert registry.get("SPY").asset_class is AssetClass.US_EQUITY


# DOGE/USD: a crypto pair the seed does not define must never be minted as a stock.
@pytest.mark.parametrize("bad", ["DOGE/USD", "TOOLONG", "AB1", "PL-TR", "BRK.BB"])
def test_a_malformed_extra_raises_instead_of_minting(bad: str):
    with pytest.raises(ValueError, match="not a plain US ticker"):
        default_registry([bad])


def test_settings_build_the_registry_from_trader_extra_symbols():
    settings = Settings(auth_disabled=True, extra_symbols="PLTR, amzn ,,NVDA")
    registry = settings.symbol_registry()
    assert registry.get("AMZN").asset_class is AssetClass.US_EQUITY
    assert registry.get("NVDA").shortable is False
    assert registry.get("BTC/USD").asset_class is AssetClass.CRYPTO


def test_unknown_tickers_still_fail_safe_without_the_setting():
    with pytest.raises(KeyError, match="unknown symbol 'PLTR'"):
        Settings(auth_disabled=True).symbol_registry().get("PLTR")
