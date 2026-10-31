"""The minimum hold: a model close of a position this Bot opened less than
``min_hold_seconds`` ago is denied by RiskGuard. The deterministic stop is never held,
and neither is any caller that does not say the intent is the model's Draft.

Why: Bot #1 bought SPY, sold it 31 minutes later on a "normal"-volume X sentiment swing
and bought it back 30 minutes after that (docs/reports/2026-10-30-sentiment-churn.md).

Seams: ``evaluate_chain`` (pure), ``RiskGuard.evaluate`` (the open lookup, per Bot),
``LoopRunner.tick`` (the model's close is held, the stop is not) and the replay's crude
book.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from tests.test_executor import FakeAlpaca
from tests.test_loop import BTC, NOW, StubLLM, StubMarketData
from tests.test_risk_chain import LIMITS, ctx, denied_by
from trader.core.types import OrderIntent, Side
from trader.loop.executor import Executor
from trader.loop.journal import DENIED, EXECUTED
from trader.loop.runner import LoopRunner
from trader.risk.guard import AccountState, RiskGuard, evaluate_chain
from trader.risk.limits import RiskLimits
from trader.store.db import Database
from trader.store.repos import PositionsRepo

HOLD_LIMITS = replace(LIMITS, min_hold_seconds=4 * 3600)
SELL = OrderIntent("BTC/USD", Side.SELL, notional=500.0)
HELD = AccountState(equity=100_000.0, positions={"BTC/USD": 0.01})
CLOSE = (
    '{"action":"close","conviction":0.7,"rationale":"sentiment turned",'
    '"side":"sell","notional_usd":100000}'
)


# --- the pure chain ---------------------------------------------------------------------


def test_a_close_inside_the_hold_is_denied():
    outcome = evaluate_chain(
        ctx(intent=SELL, account=HELD, limits=HOLD_LIMITS, opened_at=NOW - timedelta(minutes=31))
    )
    assert denied_by(outcome) == "min_hold"
    assert "the stop" in outcome.reason  # the reader learns what still protects it


def test_a_close_after_the_hold_goes():
    outcome = evaluate_chain(
        ctx(intent=SELL, account=HELD, limits=HOLD_LIMITS, opened_at=NOW - timedelta(hours=4))
    )
    assert outcome.approved


def test_no_open_time_means_no_hold():
    """The stop and every non-Draft caller pass no open time: their exits always land."""
    assert evaluate_chain(ctx(intent=SELL, account=HELD, limits=HOLD_LIMITS)).approved


def test_zero_turns_the_hold_off():
    opened = NOW - timedelta(minutes=1)
    assert evaluate_chain(ctx(intent=SELL, account=HELD, limits=LIMITS, opened_at=opened)).approved


def test_the_hold_never_touches_an_open():
    opened = NOW - timedelta(minutes=1)
    assert evaluate_chain(ctx(limits=HOLD_LIMITS, opened_at=opened)).approved


def test_kill_switches_still_come_first():
    from trader.risk.breakers import RiskState

    outcome = evaluate_chain(
        ctx(
            intent=SELL,
            account=HELD,
            limits=HOLD_LIMITS,
            opened_at=NOW - timedelta(minutes=1),
            state=RiskState(trading_paused=True, pause_reason="operator"),
        )
    )
    assert denied_by(outcome) == "kill_switch"


def test_the_setting_reaches_the_limits():
    from trader.ops.config import Settings
    from trader.risk.limits import limits_from_settings

    assert limits_from_settings(Settings()).min_hold_seconds == 14_400
    assert limits_from_settings(Settings(risk_min_hold_seconds=0)).min_hold_seconds == 0


def test_a_negative_hold_is_refused():
    import pytest

    with pytest.raises(ValueError, match="min_hold_seconds"):
        RiskLimits(min_hold_seconds=-1)


# --- RiskGuard: when did this Bot open it? -----------------------------------------------


async def journal_open(db: Database, at, *, status: str = EXECUTED, bot_id: int | None = None):
    await db.execute(
        "INSERT INTO decisions (ts, trigger, status, symbol, decision, bot_id)"
        " VALUES (%s, 'heartbeat', %s, 'BTC/USD', '{\"action\": \"open\"}', %s)",
        (at, status, bot_id),
    )


async def guard_close(db: Database, *, draft: bool, bot_id: int | None = None):
    return await RiskGuard(db, HOLD_LIMITS).evaluate(
        SELL,
        BTC,
        price=60_000.0,
        account=HELD,
        now=NOW,
        draft=draft,
        bot_id=bot_id,
    )


async def test_the_guard_holds_a_draft_close_from_the_last_executed_open(db: Database):
    await journal_open(db, NOW - timedelta(days=3))
    await journal_open(db, NOW - timedelta(minutes=31))

    outcome = await guard_close(db, draft=True)
    assert denied_by(outcome) == "min_hold"

    assert (await guard_close(db, draft=False)).approved  # the stop's call


async def test_an_open_that_never_executed_starts_no_hold(db: Database):
    for status in (DENIED, "shadow"):
        await journal_open(db, NOW - timedelta(minutes=5), status=status)
    assert (await guard_close(db, draft=True)).approved


async def test_the_hold_is_per_bot(db: Database):
    from tests.test_sleeves import seed_bot

    one = await seed_bot(db)
    two = await seed_bot(db, "bot-2", capital=10_000.0)
    for bot in (one, two):
        await db.execute(
            "INSERT INTO bot_positions (bot_id, symbol, qty, avg_entry_price)"
            " VALUES (%s, 'BTC/USD', 0.01, 60000)",
            (bot,),
        )
    await journal_open(db, NOW - timedelta(minutes=5), bot_id=two)

    assert denied_by(await guard_close(db, draft=True, bot_id=two)) == "min_hold"
    assert (await guard_close(db, draft=True, bot_id=one)).approved


# --- the loop: the model's close is held, the stop is not --------------------------------

STOP_LIMITS = RiskLimits(
    max_position_frac=0.05,
    approval_notional_usd=100_000.0,
    stop_loss_frac=0.08,
    min_hold_seconds=4 * 3600,
)


def runner_for(db: Database, broker: FakeAlpaca, llm: StubLLM) -> LoopRunner:
    return LoopRunner(
        db=db,
        market_data=StubMarketData(),
        llm=llm,
        guard=RiskGuard(db, STOP_LIMITS),
        symbols=[BTC],
        shadow=False,
        equity=100_000.0,
        executor=Executor(db, broker),
    )


async def test_the_loop_holds_a_fresh_position_against_the_model_s_close(db: Database):
    # StubMarketData quotes 159.0: an entry of 160 is a 0.6% dip, well inside the stop.
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=0.5, avg_entry_price=160.0
    )
    await journal_open(db, NOW - timedelta(minutes=31))
    broker = FakeAlpaca()

    results = await runner_for(db, broker, StubLLM([CLOSE])).tick(NOW)

    assert broker.submitted == []
    assert [(r.status, r.action) for r in results] == [(DENIED, "close")]
    row = await db.fetch_one("SELECT * FROM decisions WHERE id = %s", (results[0].decision_id,))
    assert row["decision"]["action"] == "close"  # the journal keeps what the model asked
    assert row["risk_verdicts"][-1]["check"] == "min_hold"


async def test_the_stop_closes_a_fresh_position_inside_the_hold(db: Database):
    # An entry of 200 against the 159 quote is -20.5%: past the stop on two ticks.
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=0.5, avg_entry_price=200.0
    )
    await journal_open(db, NOW - timedelta(minutes=5))
    broker = FakeAlpaca()
    hold = '{"action":"hold","conviction":0.3,"rationale":"nothing doing"}'
    runner = runner_for(db, broker, StubLLM([hold]))

    await runner.tick(NOW)
    second = await runner.tick(NOW + timedelta(seconds=60))

    assert [(r.triggers, r.status) for r in second] == [(["stop_loss"], EXECUTED)]
    assert broker.submitted[0]["qty"] == 0.5


# --- replay: the crude book honors the hold ----------------------------------------------


async def test_replay_holds_a_fresh_position_and_still_runs_the_stop(db: Database):
    from tests.test_replay_eval import OPEN, seed_hourly_bars
    from trader.eval.replay import ReplayHarness

    await seed_hourly_bars(db)
    replay_close = CLOSE.replace("100000", "1000")
    llm = StubLLM([OPEN, replay_close, replay_close, replay_close, replay_close, replay_close])
    harness = ReplayHarness(db, llm, StubMarketData(), limits=STOP_LIMITS)
    run = await harness.run(BTC, NOW - timedelta(hours=6), NOW, label="hold")

    summary = run["summary"]
    # Opened at step 1; the closes at +1h, +2h and +3h are held; +4h closes; +5h is flat.
    assert summary["min_hold_denied"] == 3
    steps = await db.fetch_all("SELECT ts, decision FROM eval_steps ORDER BY ts")
    held = [s["decision"].get("min_hold") for s in steps]
    assert held == [None, True, True, True, None, None]
    assert summary["unrealized_pnl"] == 0 and summary["realized_pnl"] != 0

    # The stop is not held: a seed opened a minute ago, far underwater, is stopped at once.
    stop_run = await harness.run(
        BTC,
        NOW - timedelta(hours=1),
        NOW,
        label="hold-stop",
        config={
            "exit_review": True,
            "exit_review_seed": {
                "qty": 0.01,
                "avg_entry_price": 70_000.0,
                "opened_at": (NOW - timedelta(hours=1, minutes=1)).isoformat(),
            },
        },
    )
    assert stop_run["summary"]["stops"] == 1
