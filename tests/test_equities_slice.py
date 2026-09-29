"""The US equities slice — SPY in the universe (long-only first), venue terms, and
per-symbol shadow so a new symbol earns its gate while the crypto slice stays live
(see docs/lessons.md#per-symbol-shadow).

PDT stance: the paper account reports PDT like a real one, but with 100k equity the
25k-minimum rule cannot bind; day-trade *counts* are Alpaca's concern, not RiskGuard's.
Revisit only if equity config ever drops below 25k (documented here deliberately).
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.test_loop import OPEN, StubMarketData
from tests.test_loop import build_runner as build_loop_runner
from trader.core.symbols import AssetClass, default_registry
from trader.core.types import OrderIntent, Side
from trader.loop.executor import venue_order_params
from trader.loop.journal import SHADOW, Journal
from trader.risk.guard import AccountState, RiskGuard
from trader.risk.limits import RiskLimits
from trader.store.db import Database

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)  # Wednesday 11:00 ET — NYSE open


def test_spy_is_registered_long_only_on_the_nyse_calendar():
    spy = default_registry().get("SPY")
    assert spy.asset_class is AssetClass.US_EQUITY
    assert spy.calendar == "nyse"
    assert spy.fractionable
    assert spy.shortable is False  # long-only first; shortability is a later gate
    assert spy.for_provider("alpaca_trading") == "SPY"
    assert spy.market_calendar.is_open(NOW)


def test_equity_venue_terms_are_notional_day_orders():
    spy = default_registry().get("SPY")
    qty, notional, tif = venue_order_params(
        OrderIntent(symbol="SPY", side=Side.BUY, notional=500.0), spy, price=500.0
    )
    assert (qty, notional, tif) == (None, 500.0, "day")  # notional requires market + day


async def test_riskguard_denies_opening_a_short_on_spy_but_never_blocks_an_exit(db: Database):
    spy = default_registry().get("SPY")
    guard = RiskGuard(db, RiskLimits(approval_notional_usd=100_000.0))

    short = await guard.evaluate(
        OrderIntent(symbol="SPY", side=Side.SELL, notional=500.0),
        spy,
        price=500.0,
        account=AccountState(equity=100_000.0, positions={}, trades_today=0),
        now=NOW,
    )
    assert not short.approved
    assert "not shortable" in short.reason

    long_open = await guard.evaluate(
        OrderIntent(symbol="SPY", side=Side.BUY, notional=500.0),
        spy,
        price=500.0,
        account=AccountState(equity=100_000.0, positions={}, trades_today=0),
        now=NOW,
    )
    assert long_open.approved

    exit_sell = await guard.evaluate(
        OrderIntent(symbol="SPY", side=Side.SELL, notional=500.0),
        spy,
        price=500.0,
        account=AccountState(equity=100_000.0, positions={"SPY": 1.0}, trades_today=0),
        now=NOW,
    )
    assert exit_sell.approved  # reducing is always allowed


# --- per-symbol shadow -------------------------------------------------------


class StubExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, outcome, symbol, *, price, decision_id=None):
        raise AssertionError("a shadow symbol must never reach the executor")


async def test_shadow_symbols_journal_shadow_while_the_build_is_live(db: Database):
    """The equities gate: SPY shadows for its sessions while ETH stays live — a
    per-symbol throttle, not the global TRADER_SHADOW_MODE flag."""
    runner = build_loop_runner(
        db,
        [OPEN],
        shadow=False,
        executor=StubExecutor(),
        shadow_symbols={"BTC/USD"},
        market_data=StubMarketData([60_000.0] * 30),
    )
    results = await runner.tick(NOW)
    assert [r.status for r in results] == [SHADOW]
    row = await Journal(db).get(results[0].decision_id)
    assert row["status"] == SHADOW
    assert row["risk_verdicts"] is not None  # risk still evaluates — shadow is a throttle
    assert await db.fetch_all("SELECT * FROM orders") == []


def test_loop_status_reports_shadow_symbols(db: Database):
    from trader.surfaces.services import build_registry

    runner = build_loop_runner(db, [], shadow=False, shadow_symbols={"SPY"})
    import asyncio

    status = asyncio.run(build_registry(db, runner).get("loop_status").handler())
    assert status["shadow"] is False
    assert status["shadow_symbols"] == ["SPY"]
