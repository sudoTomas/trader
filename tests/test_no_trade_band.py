"""The no-trade band: an open or add whose notional is below ``no_trade_band_frac`` of the
Bot's Sleeve is denied by RiskGuard. Trims, closes and the stop are never blocked.

Why: a small add pays the spread and fees for a move it barely changes (#139; the
rebalancing-band rule ai-hedge-fund's ``min_trade_pct`` carries).

Seams: ``evaluate_chain`` (pure), ``RiskGuard.evaluate`` (the Sleeve it sizes against)
and the replay's crude book.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.test_loop import BTC, NOW, StubLLM, StubMarketData
from tests.test_risk_chain import LIMITS, checks, ctx, denied_by
from trader.core.types import OrderIntent, Side
from trader.risk.guard import AccountState, RiskGuard, evaluate_chain
from trader.risk.limits import RiskLimits
from trader.risk.sleeves import Sleeve
from trader.store.db import Database

BAND = replace(LIMITS, no_trade_band_frac=0.01)  # 1% of 100,000 = 1,000
HELD = AccountState(equity=100_000.0, positions={"BTC/USD": 0.01})  # 600 at 60,000


def buy(notional: float) -> OrderIntent:
    return OrderIntent("BTC/USD", Side.BUY, notional=notional)


def sell(notional: float) -> OrderIntent:
    return OrderIntent("BTC/USD", Side.SELL, notional=notional)


# --- the pure chain ---------------------------------------------------------------------


def test_a_small_open_is_denied():
    outcome = evaluate_chain(ctx(intent=buy(500.0), limits=replace(BAND, max_position_frac=0.5)))
    assert denied_by(outcome) == "no_trade_band"
    assert "1000.00" in outcome.reason  # the band in dollars


def test_an_open_at_the_band_goes():
    outcome = evaluate_chain(ctx(intent=buy(1_000.0), limits=replace(BAND, max_position_frac=0.5)))
    assert outcome.approved
    assert "no_trade_band" in checks(outcome)


def test_a_small_add_is_denied():
    outcome = evaluate_chain(ctx(intent=buy(300.0), account=HELD, limits=BAND))
    assert denied_by(outcome) == "no_trade_band"


def test_an_add_clamped_under_the_band_by_the_position_cap_is_denied():
    """The band reads the size after the sizing clamps: 4,700 of headroom under a 5%
    cap would be fine, 400 is not."""
    nearly_full = AccountState(equity=100_000.0, positions={"BTC/USD": 4_600.0 / 60_000.0})
    outcome = evaluate_chain(ctx(intent=buy(2_000.0), account=nearly_full, limits=BAND))
    assert denied_by(outcome) == "no_trade_band"


@pytest.mark.parametrize("notional", [50.0, 600.0])
def test_trims_and_closes_are_never_banded(notional):
    outcome = evaluate_chain(ctx(intent=sell(notional), account=HELD, limits=BAND))
    assert outcome.approved
    assert "no_trade_band" not in checks(outcome)


def test_zero_turns_the_band_off():
    outcome = evaluate_chain(ctx(intent=buy(50.0), limits=replace(BAND, no_trade_band_frac=0.0)))
    assert outcome.approved
    assert "no_trade_band" not in checks(outcome)


def test_the_band_is_a_fraction_of_the_sleeve_not_the_account():
    """A 5,000 Sleeve bands at 50: a 60 open clears it though it is 0.06% of the account."""
    sleeve = Sleeve(bot_id=2, equity=5_000.0)
    limits = replace(BAND, max_position_frac=0.5)
    assert evaluate_chain(ctx(intent=buy(60.0), limits=limits, sleeve=sleeve)).approved
    small = evaluate_chain(ctx(intent=buy(40.0), limits=limits, sleeve=sleeve))
    assert denied_by(small) == "no_trade_band"
    assert "bot 2" in small.reason


def test_the_setting_reaches_the_limits():
    from trader.ops.config import Settings
    from trader.risk.limits import limits_from_settings

    assert limits_from_settings(Settings()).no_trade_band_frac == 0.01
    off = limits_from_settings(Settings(risk_no_trade_band_frac=0.0))
    assert off.no_trade_band_frac == 0.0


@pytest.mark.parametrize("frac", [-0.01, 1.0, float("nan")])
def test_a_band_outside_zero_to_one_is_refused(frac):
    with pytest.raises(ValueError, match="no_trade_band_frac"):
        RiskLimits(no_trade_band_frac=frac)


# --- RiskGuard.evaluate ------------------------------------------------------------------


async def test_the_guard_bands_opens_and_lets_the_stop_close(db: Database):
    guard = RiskGuard(db, replace(BAND, max_position_frac=0.5))
    small_open = await guard.evaluate(
        buy(500.0), BTC, price=60_000.0, account=AccountState(equity=100_000.0), now=NOW
    )
    assert denied_by(small_open) == "no_trade_band"

    # The stop's close of a small holding is an exit: it lands whatever its size.
    stop_close = await guard.evaluate(sell(600.0), BTC, price=60_000.0, account=HELD, now=NOW)
    assert stop_close.approved


async def test_risk_status_shows_the_band(db: Database, monkeypatch):
    from trader.ops.config import Settings
    from trader.surfaces.services import build_registry

    settings = Settings(auth_disabled=True, risk_no_trade_band_frac=0.02)
    monkeypatch.setattr("trader.ops.config._settings", settings)
    status = await build_registry(db=db).get("risk_status").handler()
    assert status["limits"]["no_trade_band_frac"] == 0.02


# --- replay: the crude book honors the band ----------------------------------------------


async def test_replay_bands_a_small_open(db: Database):
    from tests.test_replay_eval import OPEN, seed_hourly_bars
    from trader.eval.replay import ReplayHarness

    await seed_hourly_bars(db)
    small = OPEN.replace("1000", "500")  # under 1% of the harness's 100,000
    big = OPEN.replace("1000", "2000")
    harness = ReplayHarness(db, StubLLM([small, big]), StubMarketData(), limits=BAND)
    run = await harness.run(BTC, NOW - timedelta(hours=2), NOW, label="band")

    assert run["summary"]["no_trade_band_denied"] == 1
    steps = await db.fetch_all("SELECT decision FROM eval_steps ORDER BY ts")
    assert [s["decision"].get("no_trade_band") for s in steps] == [True, None]
    assert "fill_cost" not in steps[0]["decision"]  # the banded open never filled
    assert "fill_cost" in steps[1]["decision"]  # the 2,000 open did
