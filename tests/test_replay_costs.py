"""Replay costs: every replayed fill pays half the spread, a fee (plus Alpaca's taker fee
on crypto) and square-root market impact, and the summary reports gross and net P&L side
by side (#139). Each knob at 0 turns its component off.

The worked examples below are computed by hand; the test does not re-run the formula.

Seams: ``ReplayCosts.fill`` (pure) and the replay summary over bars with known closes and
volume.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.test_loop import StubLLM, StubMarketData
from tests.test_replay_eval import BTC, NOW, OPEN
from trader.core.symbols import AssetClass
from trader.eval.costs import ReplayCosts, costs_from_settings
from trader.store.db import Database
from trader.store.repos import BarsRepo

# The defaults the issue names: 5 bp / 10 bp half-spread, 2 bp fee, 25 bp crypto taker
# fee, and an impact coefficient of 1.0.
COSTS = ReplayCosts(
    half_spread_bps_equity=5.0,
    half_spread_bps_crypto=10.0,
    fee_bps=2.0,
    crypto_taker_fee_bps=25.0,
    impact_coef=1.0,
)


# --- the cost of one fill ---------------------------------------------------------------


def test_an_equity_fill_without_a_quote_pays_the_fallback_spread_fee_and_impact():
    # 10,000 of SPY, 1,000,000 of daily dollar volume, a 2% daily move:
    # spread 10,000 * 5 bp = 5.00; fee 10,000 * 2 bp = 2.00;
    # impact 10,000 * 1.0 * 0.02 * sqrt(10,000 / 1,000,000) = 10,000 * 0.002 = 20.00.
    cost = COSTS.fill(AssetClass.US_EQUITY, 10_000.0, adv_usd=1_000_000.0, daily_vol=0.02)
    assert cost.spread == pytest.approx(5.0)
    assert cost.fee == pytest.approx(2.0)
    assert cost.impact == pytest.approx(20.0)
    assert cost.total == pytest.approx(27.0)


def test_a_crypto_fill_pays_the_wider_spread_and_the_taker_fee():
    # 1,000 of BTC: spread 1,000 * 10 bp = 1.00; fee 1,000 * (2 + 25) bp = 2.70; no
    # liquidity reading, so no impact.
    cost = COSTS.fill(AssetClass.CRYPTO, 1_000.0)
    assert (cost.spread, cost.fee, cost.impact) == pytest.approx((1.0, 2.7, 0.0))


def test_a_commodity_etf_pays_the_equity_rates():
    cost = COSTS.fill(AssetClass.COMMODITY_ETF, 1_000.0)
    assert (cost.spread, cost.fee) == pytest.approx((0.5, 0.2))


def test_a_real_quote_sets_the_spread():
    # Bid 99.90 / ask 100.10: half of 0.20 on a 100.00 mid is 10 bp; 1,000 pays 1.00.
    cost = COSTS.fill(AssetClass.US_EQUITY, 1_000.0, bid=99.90, ask=100.10)
    assert cost.spread == pytest.approx(1.0)


def test_a_locked_quote_falls_back_to_the_class_rate():
    """Replay's quote is the last close on both sides: that is no quote, not a free fill."""
    cost = COSTS.fill(AssetClass.US_EQUITY, 1_000.0, bid=100.0, ask=100.0)
    assert cost.spread == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("knob", "component"),
    [
        ("half_spread_bps_equity", "spread"),
        ("fee_bps", "fee"),
        ("impact_coef", "impact"),
    ],
)
def test_zero_turns_each_equity_component_off(knob, component):
    off = ReplayCosts(**{**COSTS.as_dict(), knob: 0.0})
    cost = off.fill(
        AssetClass.US_EQUITY, 10_000.0, bid=99.9, ask=100.1, adv_usd=1e6, daily_vol=0.02
    )
    assert getattr(cost, component) == 0.0


def test_zero_taker_fee_leaves_crypto_the_plain_fee():
    off = ReplayCosts(**{**COSTS.as_dict(), "crypto_taker_fee_bps": 0.0})
    assert off.fill(AssetClass.CRYPTO, 1_000.0).fee == pytest.approx(0.2)


def test_the_default_model_is_free():
    assert ReplayCosts().fill(AssetClass.CRYPTO, 1_000.0, adv_usd=1e3, daily_vol=0.5).total == 0


@pytest.mark.parametrize("knob", ["fee_bps", "impact_coef"])
@pytest.mark.parametrize("value", [-1.0, float("nan")])
def test_a_negative_or_unreadable_knob_is_refused(knob, value):
    with pytest.raises(ValueError, match=knob):
        ReplayCosts(**{knob: value})


def test_the_settings_carry_the_issue_defaults():
    from trader.ops.config import Settings

    assert costs_from_settings(Settings(auth_disabled=True)) == COSTS


# --- the replay summary: gross and net side by side ---------------------------------------


async def seed_choppy_bars(db: Database, *, count: int = 120) -> None:
    """BTC hourly closes alternating 60,000 / 60,600 (even / odd bar), 2 units a bar,
    ending one hour before NOW. Every 20-return window then has a per-bar std of
    ln(1.01) * sqrt(20/19) = 0.0102088, a daily move (x sqrt 24) of 0.0500128, and a
    daily volume of 48 units."""
    await BarsRepo(db).upsert_many(
        [
            {
                "symbol": "BTC/USD",
                "interval": "1h",
                "ts": NOW - timedelta(hours=count - i),
                "open": 60_000.0 if i % 2 == 0 else 60_600.0,
                "high": 60_600.0,
                "low": 60_000.0,
                "close": 60_000.0 if i % 2 == 0 else 60_600.0,
                "volume": 2.0,
            }
            for i in range(count)
        ]
    )


CLOSE_ALL = (
    '{"action":"close","conviction":0.7,"rationale":"done","side":"sell","notional_usd":1000}'
)


async def test_replay_reports_gross_and_net_pnl(db: Database):
    from trader.eval.replay import ReplayHarness

    await seed_choppy_bars(db)
    harness = ReplayHarness(db, StubLLM([OPEN, CLOSE_ALL]), StubMarketData(), costs=COSTS)
    run = await harness.run(BTC, NOW - timedelta(hours=2), NOW, label="costs")

    steps = await db.fetch_all("SELECT price, decision FROM eval_steps ORDER BY ts")
    assert [float(s["price"]) for s in steps] == [60_600.0, 60_000.0]

    # Buy 1,000 at 60,600 (qty 0.01650165), sell it at 60,000 (990.0990 of notional).
    # Spread 10 bp:  1.000000 + 0.990099            = 1.990099
    # Fee 27 bp:     2.700000 + 2.673267            = 5.373267
    # Impact, participation 0.01650165 / 48 = 3.4378e-4, sqrt = 0.0185413:
    #                1,000 * 0.0500128 * 0.0185413 = 0.927309
    #                990.0990 * 0.0500128 * 0.0185413 = 0.918128   -> 1.845437
    # Total 9.208803; gross (60,000 - 60,600) * 0.01650165 = -9.900990.
    summary = run["summary"]
    assert summary["gross_pnl"] == pytest.approx(-9.900990, abs=1e-5)
    costs = summary["trading_costs"]
    assert costs["spread"] == pytest.approx(1.990099, abs=1e-5)
    assert costs["fee"] == pytest.approx(5.373267, abs=1e-5)
    assert costs["impact"] == pytest.approx(1.845437, abs=1e-5)
    assert costs["total"] == pytest.approx(9.208803, abs=1e-5)
    assert summary["net_pnl"] == pytest.approx(-19.109793, abs=1e-5)
    assert summary["realized_pnl"] == pytest.approx(-9.900990, abs=1e-5)  # still gross

    # Each fill step carries what it paid.
    assert steps[0]["decision"]["fill_cost"]["total"] == pytest.approx(
        1.0 + 2.7 + 0.927309, abs=1e-5
    )


async def test_replay_without_a_cost_model_is_free(db: Database):
    from trader.eval.replay import ReplayHarness

    await seed_choppy_bars(db)
    harness = ReplayHarness(db, StubLLM([OPEN, CLOSE_ALL]), StubMarketData())
    summary = (await harness.run(BTC, NOW - timedelta(hours=2), NOW, label="free"))["summary"]
    assert summary["trading_costs"]["total"] == 0
    assert summary["net_pnl"] == summary["gross_pnl"]


def test_the_api_replays_with_the_configured_costs(monkeypatch):
    from trader.ops.config import Settings
    from trader.surfaces.api.app import build_evaluator

    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    harness = build_evaluator(None, Settings(), llm=object())  # type: ignore[arg-type]
    assert harness._costs == COSTS
