"""The commodity ETF slice — GLD / SLV / USO in the universe, long-only, on the NYSE
calendar, through the same RiskGuard chain and the same equities data path (#4).

Nothing here flips a symbol live: the operator lists a new ticker in
TRADER_SHADOW_SYMBOLS first and it earns its flip in the journal
(see docs/lessons.md#per-symbol-shadow).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.test_loop import StubMarketData
from trader.core.symbols import AssetClass, default_registry
from trader.core.types import OrderIntent, Side
from trader.data.alpaca import AlpacaData
from trader.data.composite import CompositeMarketData
from trader.loop.context import ContextBuilder
from trader.loop.executor import venue_order_params
from trader.loop.scheduler import HEARTBEAT, Mark, SignalScheduler, TriggerConfig
from trader.risk.guard import AccountState, RiskGuard
from trader.risk.limits import RiskLimits
from trader.store.db import Database

ETFS = ("GLD", "SLV", "USO")
NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)  # Wednesday 11:00 ET — NYSE open
AFTER_HOURS = datetime(2026, 8, 12, 21, 0, tzinfo=UTC)  # Wednesday 17:00 ET — closed
SATURDAY = datetime(2026, 8, 15, 15, 0, tzinfo=UTC)


# --- registry ----------------------------------------------------------------


@pytest.mark.parametrize("ticker", ETFS)
def test_commodity_etfs_are_registered_long_only_on_the_nyse_calendar(ticker: str):
    etf = default_registry().get(ticker)
    assert etf.asset_class is AssetClass.COMMODITY_ETF
    assert etf.calendar == "nyse"
    assert etf.fractionable
    assert etf.shortable is False  # exposure vehicle, never a short candidate
    assert etf.min_notional == 1.0
    # Alpaca spells ETFs like equities on both the data and the trading API.
    assert etf.for_provider("alpaca_data") == ticker
    assert etf.for_provider("alpaca_trading") == ticker


def test_commodity_etf_class_lists_exactly_the_three_seeds():
    reg = default_registry()
    assert {s.ticker for s in reg.by_class(AssetClass.COMMODITY_ETF)} == set(ETFS)
    # The slice joins the universe without disturbing the seeded crypto + SPY entries.
    assert {s.ticker for s in reg.all()} == {"BTC/USD", "ETH/USD", "SPY", *ETFS}


# --- calendar gating ---------------------------------------------------------


@pytest.mark.parametrize("ticker", ETFS)
def test_commodity_etfs_trade_only_during_the_nyse_session(ticker: str):
    calendar = default_registry().get(ticker).market_calendar
    assert calendar.name == "nyse"
    assert calendar.is_open(NOW)
    assert not calendar.is_open(AFTER_HOURS)
    assert not calendar.is_open(SATURDAY)
    assert calendar.session_day(NOW) == NOW.date()


# --- riskguard: long-only + session policy -----------------------------------


async def test_riskguard_denies_an_etf_short_and_passes_a_long_open(db: Database):
    gld = default_registry().get("GLD")
    guard = RiskGuard(db, RiskLimits(approval_notional_usd=100_000.0))
    flat = AccountState(equity=100_000.0, positions={}, trades_today=0)

    short = await guard.evaluate(
        OrderIntent(symbol="GLD", side=Side.SELL, notional=500.0),
        gld,
        price=250.0,
        account=flat,
        now=NOW,
    )
    assert not short.approved
    assert short.verdicts[-1].check == "class_policy"
    assert "not shortable" in short.reason

    long_open = await guard.evaluate(
        OrderIntent(symbol="GLD", side=Side.BUY, notional=500.0),
        gld,
        price=250.0,
        account=flat,
        now=NOW,
    )
    assert long_open.approved
    assert long_open.executable().notional == 500.0

    exit_sell = await guard.evaluate(
        OrderIntent(symbol="GLD", side=Side.SELL, notional=500.0),
        gld,
        price=250.0,
        account=AccountState(equity=100_000.0, positions={"GLD": 2.0}, trades_today=0),
        now=NOW,
    )
    assert exit_sell.approved  # reducing a long is never a short


@pytest.mark.parametrize("when", [AFTER_HOURS, SATURDAY], ids=["after_hours", "weekend"])
async def test_riskguard_denies_an_etf_open_while_the_venue_is_closed(db: Database, when: datetime):
    slv = default_registry().get("SLV")
    guard = RiskGuard(db, RiskLimits(approval_notional_usd=100_000.0))
    outcome = await guard.evaluate(
        OrderIntent(symbol="SLV", side=Side.BUY, notional=500.0),
        slv,
        price=30.0,
        account=AccountState(equity=100_000.0, positions={}, trades_today=0),
        now=when,
    )
    assert not outcome.approved
    assert outcome.verdicts[-1].check == "class_policy"
    assert "nyse venue is closed" in outcome.reason


# --- venue terms + data path (unchanged from equities) -----------------------


def test_etf_venue_terms_are_notional_day_orders():
    uso = default_registry().get("USO")
    qty, notional, tif = venue_order_params(
        OrderIntent(symbol="USO", side=Side.BUY, notional=500.0), uso, price=80.0
    )
    assert (qty, notional, tif) == (None, 500.0, "day")


async def test_etf_bars_and_quotes_take_the_equities_path_on_alpaca():
    """SIP historical bars behind the delayed end, IEX latest quote — exactly SPY's path."""
    gld = default_registry().get("GLD")
    seen: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.path == "/v2/stocks/bars":
            seen["bars"] = params
            return httpx.Response(200, json={"bars": {"GLD": []}})
        if request.url.path == "/v2/stocks/quotes/latest":
            seen["quote"] = params
            return httpx.Response(
                200,
                json={"quotes": {"GLD": {"t": "2026-08-12T15:00:00Z", "bp": 249.9, "ap": 250.1}}},
            )
        return httpx.Response(404, json={"message": f"unexpected {request.url.path}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")
    data = AlpacaData("k", "s", client=client)

    assert await data.get_bars(gld, "1h", 10) == []
    assert seen["bars"]["symbols"] == "GLD"
    assert seen["bars"]["feed"] == "sip"
    assert "end" in seen["bars"]

    quote = await data.get_latest_quote(gld)
    assert seen["quote"] == {"symbols": "GLD", "feed": "iex"}
    assert quote.symbol == "GLD" and quote.mid == pytest.approx(250.0)


async def test_composite_routes_commodity_etfs_to_the_equities_chain():
    equities = StubMarketData([100.0] * 5)
    crypto = StubMarketData([1.0] * 5)
    composite = CompositeMarketData(
        {
            AssetClass.CRYPTO: [crypto],
            AssetClass.US_EQUITY: [equities],
            AssetClass.COMMODITY_ETF: [equities],
        }
    )
    bars = await composite.get_bars(default_registry().get("SLV"), "1h", 5)
    assert equities.bar_calls == 1 and crypto.bar_calls == 0
    assert all(b.symbol == "SLV" for b in bars)


# --- context build over mocked bars -----------------------------------------


async def test_context_builds_for_gld_over_mocked_bars():
    gld = default_registry().get("GLD")
    data = StubMarketData([240.0 + i * 0.5 for i in range(60)])
    ctx = await ContextBuilder(data).build(gld, now=NOW, triggers=[], equity=100_000.0)

    assert data.bar_calls == 1  # one fetch, indicators computed locally
    assert ctx.symbol is gld
    assert ctx.price == pytest.approx(269.5)
    assert all(b.symbol == "GLD" and b.interval == "1h" for b in ctx.bars["1h"])
    indicators = ctx.indicators["1h"]
    assert indicators.rsi_14 is not None and indicators.realized_vol_20 is not None
    assert ctx.primary_vol == indicators.realized_vol_20
    prompt = ctx.to_prompt()
    assert "Symbol: GLD (commodity_etf)" in prompt
    assert "rsi_14" in prompt


# --- scheduler heartbeat for the class --------------------------------------


def test_commodity_etf_heartbeat_cadence_is_the_equity_cadence():
    assert TriggerConfig().heartbeat_seconds[AssetClass.COMMODITY_ETF] == 1800


def test_commodity_etf_heartbeat_fires_on_cadence_and_only_while_open():
    uso = default_registry().get("USO")
    scheduler = SignalScheduler([uso], TriggerConfig(price_move_pct=9.0))

    first = scheduler.evaluate(NOW, {"USO": Mark(price=80.0)})
    assert [(t.type, t.symbol) for t in first] == [(HEARTBEAT, "USO")]
    assert first[0].detail == {"cadence_seconds": 1800}

    scheduler.mark_dispatched(first, NOW)
    assert scheduler.evaluate(NOW + timedelta(minutes=20), {"USO": Mark(price=80.0)}) == []
    later = scheduler.evaluate(NOW + timedelta(minutes=30), {"USO": Mark(price=80.0)})
    assert [t.type for t in later] == [HEARTBEAT]

    # A closed venue never wakes the trader, whatever the cadence says.
    assert scheduler.evaluate(SATURDAY, {"USO": Mark(price=80.0)}) == []
    assert scheduler.evaluate(AFTER_HOURS, {"USO": Mark(price=80.0)}) == []
