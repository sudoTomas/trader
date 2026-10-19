"""Sleeves (#125, ADR 0003): each Bot trades a Sleeve, checked by the one chain.

The Sleeve is data in ``RiskContext`` beside the account, never a second checker: sizing
reads the Sleeve's equity, "is this reducing" and the full close read the Sleeve's
position, throughput is checked for the Bot and for the account, and a halt is either
the Account's (every Bot) or one Bot's own. Bot #1's Sleeve is the whole account, so its
verdicts are identical to the chain without a Sleeve.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_executor import FakeAlpaca
from tests.test_risk_chain import (
    APPROVAL_LIMITS,
    BTC,
    GLD,
    KELLY_BINDS,
    LIMITS,
    NOW,
    SATURDAY,
    SPY,
    ctx,
    denied_by,
)
from trader.brokers.base import BrokerPosition, OrderEvent
from trader.core.types import OrderIntent, Side
from trader.loop.bots import BOT_ONE, BotDesk, bot_one_spec
from trader.loop.executor import Executor
from trader.ops.config import Settings
from trader.risk.breakers import RiskState
from trader.risk.guard import AccountState, RiskGuard, RiskOutcome, Sleeve, evaluate_chain
from trader.store.db import Database
from trader.store.repos import BotPositionsRepo, PositionsRepo


def whole_account(account: AccountState, bot_id: int = 1, **kw) -> Sleeve:
    """Bot #1's Sleeve as the shell builds it: the account's equity, its own ledger
    mirroring the account (every fill so far was Bot #1's)."""
    return Sleeve(
        bot_id=bot_id,
        equity=account.equity,
        positions=dict(account.positions),
        trades_today=account.trades_today,
        **kw,
    )


# --- verdict parity for Bot #1 ------------------------------------------------------------

PARITY_CORPUS = [
    {},
    {"intent": OrderIntent("BTC/USD", Side.BUY, notional=50_000.0)},
    {"intent": OrderIntent("BTC/USD", Side.BUY, qty=1.0)},
    {"intent": OrderIntent("BTC/USD", Side.BUY, notional=1.0)},
    {"intent": OrderIntent("BTC/USD", Side.SELL, notional=500.0)},
    {"limits": KELLY_BINDS, "intent": OrderIntent("BTC/USD", Side.BUY, notional=50_000.0)},
    {"limits": KELLY_BINDS, "realized_vol": 0.08},
    {"limits": APPROVAL_LIMITS, "intent": OrderIntent("BTC/USD", Side.BUY, notional=5_000.0)},
    {
        "limits": replace(APPROVAL_LIMITS, park_above_usd=2_000.0),
        "intent": OrderIntent("BTC/USD", Side.BUY, notional=5_000.0),
    },
    {"state": RiskState(trading_paused=True, pause_reason="operator")},
    {"state": RiskState(drift_detected=True)},
    {"state": RiskState(halted_reason="daily_loss")},
    {"live": True},
    {"price": float("nan")},
    {"account": AccountState(equity=0.0)},
    {"account": AccountState(equity=40.0)},
    {"account": AccountState(equity=100_000.0, trades_today=5)},
    {"account": AccountState(equity=100_000.0, positions={"BTC/USD": 0.05})},
    {"account": AccountState(equity=100_000.0, positions={"BTC/USD": 0.1})},
    {
        "account": AccountState(equity=100_000.0, positions={"A": 1.0, "B": 1.0, "C": 1.0}),
    },
    {
        "account": AccountState(equity=100_000.0, positions={"BTC/USD": 0.5}),
        "intent": OrderIntent("BTC/USD", Side.SELL, notional=29_700.0),
    },
    {
        "account": AccountState(equity=100_000.0, positions={"BTC/USD": 0.5}),
        "intent": OrderIntent("BTC/USD", Side.SELL, qty=0.2),
        "state": RiskState(halted_reason="weekly_drawdown"),
    },
    {"stopped_at": NOW - timedelta(minutes=5)},
    {"symbol": SPY, "intent": OrderIntent("SPY", Side.BUY, notional=500.0), "price": 600.0},
    {"symbol": SPY, "intent": OrderIntent("SPY", Side.SELL, notional=500.0), "price": 600.0},
    {"symbol": SPY, "now": SATURDAY, "intent": OrderIntent("SPY", Side.BUY, notional=500.0)},
    {"symbol": GLD, "intent": OrderIntent("GLD", Side.SELL, notional=500.0), "price": 200.0},
    {
        "symbol": replace(SPY, fractionable=False),
        "intent": OrderIntent("SPY", Side.BUY, notional=1_000.0),
        "price": 600.0,
    },
]


@pytest.mark.parametrize("overrides", PARITY_CORPUS)
def test_bot_one_verdicts_are_identical_with_its_whole_account_sleeve(overrides):
    before = evaluate_chain(ctx(**overrides))
    base = ctx(**overrides)
    after = evaluate_chain(replace(base, sleeve=whole_account(base.account)))
    assert after.verdict_dicts == before.verdict_dicts
    assert (after.approved, after.intent, after.reason) == (
        before.approved,
        before.intent,
        before.reason,
    )


# --- the Sleeve's position decides "reducing" and the full close ------------------------

HELD = AccountState(equity=100_000.0, positions={"BTC/USD": 0.5, "SPY": 10.0})


@pytest.mark.parametrize(
    ("intent", "symbol", "price"),
    [
        (OrderIntent("BTC/USD", Side.SELL, qty=0.5), BTC, 60_000.0),  # close the long
        (OrderIntent("BTC/USD", Side.SELL, qty=0.1), BTC, 60_000.0),  # reduce it
        (OrderIntent("GLD", Side.SELL, qty=1.0), GLD, 200.0),
    ],
)
def test_a_bot_cannot_reduce_or_close_what_its_sleeve_does_not_hold(intent, symbol, price):
    held = AccountState(equity=100_000.0, positions={"BTC/USD": 0.5, "GLD": 5.0})
    other_bots = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0)  # holds nothing
    outcome = evaluate_chain(
        ctx(intent=intent, symbol=symbol, price=price, account=held, sleeve=other_bots)
    )
    # Not an exit for this Bot: a long-only sell would open a short, so it is denied.
    assert denied_by(outcome) == "class_policy"


def test_a_shortable_symbol_the_sleeve_does_not_hold_is_an_open_not_a_close():
    sleeve = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0)
    outcome = evaluate_chain(
        ctx(
            intent=OrderIntent("SPY", Side.SELL, qty=10.0),
            symbol=SPY,
            price=600.0,
            account=HELD,
            sleeve=sleeve,
        )
    )
    # Sized as an open against the Sleeve (5% of 10k), never a whole-holding close.
    assert "reducing_position" not in [v.check for v in outcome.verdicts]
    assert outcome.executable().qty == pytest.approx(500.0 / 600.0)


def test_the_full_close_is_the_sleeve_s_holding_not_the_account_s():
    sleeve = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0, positions={"BTC/USD": 0.1})
    outcome = evaluate_chain(
        ctx(intent=OrderIntent("BTC/USD", Side.SELL, qty=0.5), account=HELD, sleeve=sleeve)
    )
    assert outcome.verdicts[-1].check == "full_close"
    assert outcome.executable().qty == pytest.approx(0.1)


# --- sizing against a fixed-dollar Sleeve ------------------------------------------------


def test_sizing_clamps_to_a_fixed_dollar_sleeve():
    sleeve = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0)
    outcome = evaluate_chain(
        ctx(intent=OrderIntent("BTC/USD", Side.BUY, notional=50_000.0), sleeve=sleeve)
    )
    assert outcome.executable().notional == pytest.approx(500.0)  # 5% of the 10k Sleeve
    assert "5% cap" in next(v.reason for v in outcome.verdicts if v.check == "max_position_frac")


def test_kelly_reads_the_sleeve_s_equity():
    sleeve = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0)
    outcome = evaluate_chain(
        ctx(
            limits=KELLY_BINDS,
            intent=OrderIntent("BTC/USD", Side.BUY, notional=50_000.0),
            sleeve=sleeve,
        )
    )
    assert outcome.executable().notional == pytest.approx(625.0)  # 6.25% of 10k


def test_the_position_cap_counts_what_the_sleeve_already_holds():
    sleeve = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0, positions={"BTC/USD": 0.01})
    outcome = evaluate_chain(ctx(intent=OrderIntent("BTC/USD", Side.BUY, notional=500.0)))
    assert outcome.approved  # the account alone has room
    capped = evaluate_chain(
        ctx(intent=OrderIntent("BTC/USD", Side.BUY, notional=500.0), sleeve=sleeve)
    )
    assert denied_by(capped) == "max_position_frac"  # $600 held >= $500 cap


def test_unreadable_sleeve_equity_is_a_deny():
    sleeve = Sleeve(bot_id=2, equity=float("nan"), capital=10_000.0)
    outcome = evaluate_chain(ctx(sleeve=sleeve))
    assert denied_by(outcome) == "input_sanity"
    assert "sleeve" in outcome.reason


# --- throughput at both levels ------------------------------------------------------------


def test_concurrency_is_checked_for_the_bot_and_for_the_account():
    full_account = AccountState(equity=100_000.0, positions={"A": 1.0, "B": 1.0, "C": 1.0})
    empty_sleeve = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0)
    assert denied_by(evaluate_chain(ctx(account=full_account, sleeve=empty_sleeve))) == (
        "max_concurrent"
    )

    limits = replace(LIMITS, max_concurrent_positions=2)
    busy_sleeve = Sleeve(
        bot_id=2, equity=10_000.0, capital=10_000.0, positions={"A": 1.0, "B": 1.0}
    )
    roomy_account = AccountState(equity=100_000.0, positions={"A": 1.0})
    outcome = evaluate_chain(ctx(limits=limits, account=roomy_account, sleeve=busy_sleeve))
    assert denied_by(outcome) == "max_concurrent" and "bot 2" in outcome.reason


def test_trades_today_are_checked_for_the_bot_and_for_the_account():
    spent_account = AccountState(equity=100_000.0, trades_today=5)
    fresh = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0)
    assert denied_by(evaluate_chain(ctx(account=spent_account, sleeve=fresh))) == (
        "max_trades_per_day"
    )
    spent_bot = Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0, trades_today=5)
    outcome = evaluate_chain(ctx(account=AccountState(equity=100_000.0), sleeve=spent_bot))
    assert denied_by(outcome) == "max_trades_per_day" and "bot 2" in outcome.reason


# --- Account halt vs a Bot's own halt ------------------------------------------------------

ACCOUNT_HALT = RiskState(halted_reason="daily_loss")
BOTS = [
    Sleeve(bot_id=1, equity=100_000.0, positions={"BTC/USD": 0.001}),
    Sleeve(bot_id=2, equity=10_000.0, capital=10_000.0, positions={"BTC/USD": 0.001}),
]


@pytest.mark.parametrize("sleeve", BOTS)
def test_an_account_halt_blocks_every_bot_s_opens_but_not_its_exits(sleeve):
    account = AccountState(equity=100_000.0, positions={"BTC/USD": 0.002})
    opening = evaluate_chain(ctx(account=account, sleeve=sleeve, state=ACCOUNT_HALT))
    assert denied_by(opening) == "breaker"

    exiting = evaluate_chain(
        ctx(
            account=account,
            sleeve=sleeve,
            state=ACCOUNT_HALT,
            intent=OrderIntent("BTC/USD", Side.SELL, qty=sleeve.positions["BTC/USD"]),
        )
    )
    assert exiting.approved
    assert exiting.executable().qty == pytest.approx(sleeve.positions["BTC/USD"])


def test_a_bot_s_own_breaker_blocks_only_that_bot_s_opens():
    account = AccountState(equity=100_000.0, positions={"BTC/USD": 0.001})
    halted = Sleeve(
        bot_id=2,
        equity=9_000.0,
        capital=10_000.0,
        positions={"BTC/USD": 0.001},
        state=RiskState(halted_reason="daily_loss"),
    )
    outcome = evaluate_chain(ctx(account=account, sleeve=halted))
    assert denied_by(outcome) == "breaker" and "bot 2" in outcome.reason
    exit_ = evaluate_chain(
        ctx(account=account, sleeve=halted, intent=OrderIntent("BTC/USD", Side.SELL, qty=0.001))
    )
    assert exit_.approved
    # Another Bot on the same account is untouched.
    other = Sleeve(bot_id=1, equity=100_000.0)
    assert evaluate_chain(ctx(account=account, sleeve=other)).approved


def test_a_bot_pause_blocks_only_that_bot():
    paused = Sleeve(
        bot_id=2,
        equity=10_000.0,
        capital=10_000.0,
        state=RiskState(trading_paused=True, pause_reason="operator"),
    )
    outcome = evaluate_chain(ctx(sleeve=paused))
    assert denied_by(outcome) == "kill_switch" and "bot 2" in outcome.reason
    assert evaluate_chain(ctx(sleeve=Sleeve(bot_id=1, equity=100_000.0))).approved


# --- the store: ledger, fold, reconcile ---------------------------------------------------


def settings(**kwargs) -> Settings:
    return Settings(_env_file=None, auth_disabled=True, **kwargs)


async def seed_bot(db: Database, name: str = BOT_ONE, capital: float | None = None) -> int:
    spec = bot_one_spec(settings(loop_symbols="BTC/USD"))
    spec["sleeve"] = {"capital_usd": capital}
    return (await BotDesk(db).seed(name, spec))["id"]


async def fill(executor: Executor, client_order_id: str, qty: float, price: float, n: int):
    await executor.apply_event(
        OrderEvent(
            event="fill",
            client_order_id=client_order_id,
            broker_order_id=f"brk-{n}",
            status="filled",
            ts=NOW,
            filled_qty=qty,
            fill_price=price,
            execution_id=f"exec-{n}",
        )
    )


async def test_bot_one_spec_carries_a_whole_account_sleeve():
    assert bot_one_spec(settings())["sleeve"] == {"capital_usd": None}


async def test_fills_fold_into_the_position_and_the_right_sleeve(db: Database):
    one = await seed_bot(db)
    two = await seed_bot(db, "bot-2", capital=10_000.0)
    guard = RiskGuard(db, LIMITS)
    executor = Executor(db, FakeAlpaca())
    account = AccountState(equity=100_000.0)

    async def buy(bot_id: int, notional: float):
        outcome = await guard.evaluate(
            OrderIntent("BTC/USD", Side.BUY, notional=notional),
            BTC,
            price=60_000.0,
            account=account,
            now=NOW,
            realized_vol=0.02,
            bot_id=bot_id,
        )
        return await executor.execute(outcome, BTC, price=60_000.0, bot_id=bot_id)

    first = await buy(one, 3_000.0)
    second = await buy(two, 300.0)
    await fill(executor, first.client_order_id, 0.05, 60_000.0, 1)
    await fill(executor, second.client_order_id, 0.005, 60_000.0, 2)

    sleeves = BotPositionsRepo(db)
    assert float((await sleeves.get(one, "BTC/USD"))["qty"]) == pytest.approx(0.05)
    assert float((await sleeves.get(two, "BTC/USD"))["qty"]) == pytest.approx(0.005)
    total = await PositionsRepo(db).get("BTC/USD")
    assert float(total["qty"]) == pytest.approx(0.055)


async def test_a_sell_folds_realized_pnl_into_the_sleeve(db: Database):
    bot = await seed_bot(db)
    executor = Executor(db, FakeAlpaca())
    await BotPositionsRepo(db).upsert(
        bot_id=bot, symbol="BTC/USD", qty=0.1, avg_entry_price=50_000.0
    )
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=0.1, avg_entry_price=50_000.0
    )
    order_id = await db.fetch_one(
        "INSERT INTO orders (client_order_id, symbol, asset_class, side, qty, bot_id)"
        " VALUES ('sell-1', 'BTC/USD', 'crypto', 'sell', 0.04, %s) RETURNING id",
        (bot,),
    )
    assert order_id
    await fill(executor, "sell-1", 0.04, 60_000.0, 1)
    row = await BotPositionsRepo(db).get(bot, "BTC/USD")
    assert float(row["qty"]) == pytest.approx(0.06)
    assert float(row["avg_entry_price"]) == pytest.approx(50_000.0)
    assert float(row["realized_pnl"]) == pytest.approx(400.0)


async def test_the_sleeve_takes_the_same_venue_corrected_delta_as_the_account(db: Database):
    """Crypto fees come out of the asset: the venue's position wins for the account, and
    the Sleeve moves by the same amount, so the two never drift apart."""
    bot = await seed_bot(db)
    await db.execute(
        "INSERT INTO orders (client_order_id, symbol, asset_class, side, qty, bot_id)"
        " VALUES ('buy-1', 'ETH/USD', 'crypto', 'buy', 1, %s)",
        (bot,),
    )
    await Executor(db, FakeAlpaca()).apply_event(
        OrderEvent(
            event="fill",
            client_order_id="buy-1",
            broker_order_id="b",
            status="filled",
            ts=NOW,
            filled_qty=1.0,
            fill_price=2_000.0,
            execution_id="e1",
            position_qty=0.9975,
        )
    )
    assert float((await BotPositionsRepo(db).get(bot, "ETH/USD"))["qty"]) == pytest.approx(0.9975)


async def test_sleeves_that_do_not_sum_to_the_positions_are_an_account_halt(db: Database):
    from trader.risk.breakers import BreakerStore

    bot = await seed_bot(db)
    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.5)
    await BotPositionsRepo(db).upsert(bot_id=bot, symbol="BTC/USD", qty=0.3)
    venue = FakeAlpaca(positions=[BrokerPosition("BTC/USD", qty=0.5, qty_available=0.5)])

    result = await Executor(db, venue).reconcile_positions()
    assert result["drift"] == {}  # positions match the venue...
    assert result["sleeve_drift"]["BTC/USD"] == {"positions": 0.5, "sleeves": 0.3}
    state = await BreakerStore(db).load()
    assert state.drift_detected  # ...but the Sleeves do not add up: Account halt


async def test_sleeves_that_sum_to_positions_reconcile_clean(db: Database):
    from trader.risk.breakers import BreakerStore

    one = await seed_bot(db)
    two = await seed_bot(db, "bot-2", capital=10_000.0)
    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.5)
    await BotPositionsRepo(db).upsert(bot_id=one, symbol="BTC/USD", qty=0.3)
    await BotPositionsRepo(db).upsert(bot_id=two, symbol="BTC/USD", qty=0.2)
    venue = FakeAlpaca(positions=[BrokerPosition("BTC/USD", qty=0.5, qty_available=0.5)])
    result = await Executor(db, venue).reconcile_positions()
    assert result["drift"] == {} and result["sleeve_drift"] == {}
    assert not (await BreakerStore(db).load()).drift_detected


async def test_adopting_the_venue_rebalances_the_sleeves(db: Database):
    bot = await seed_bot(db)
    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.5)
    await BotPositionsRepo(db).upsert(bot_id=bot, symbol="BTC/USD", qty=0.5)
    venue = FakeAlpaca(
        positions=[
            BrokerPosition("BTC/USD", qty=0.2, qty_available=0.2, asset_class="crypto"),
            BrokerPosition("ETH/USD", qty=1.0, qty_available=1.0, asset_class="crypto"),
        ]
    )
    executor = Executor(db, venue)
    await executor.adopt_venue_positions()
    sleeves = BotPositionsRepo(db)
    assert float((await sleeves.get(bot, "BTC/USD"))["qty"]) == pytest.approx(0.2)
    assert float((await sleeves.get(1, "ETH/USD"))["qty"]) == pytest.approx(1.0)
    assert (await executor.reconcile_positions())["sleeve_drift"] == {}


async def test_the_crypto_sell_clamp_reads_the_sleeve(db: Database):
    two = await seed_bot(db, "bot-2", capital=10_000.0)
    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.5)
    await BotPositionsRepo(db).upsert(bot_id=two, symbol="BTC/USD", qty=0.01)
    # A notional sell whose mark conversion (0.015) overshoots what the Sleeve holds.
    outcome = RiskOutcome(True, OrderIntent("BTC/USD", Side.SELL, notional=900.0), [])
    broker = FakeAlpaca()
    await Executor(db, broker).execute(outcome, BTC, price=60_000.0, bot_id=two)
    assert broker.submitted[0]["qty"] == pytest.approx(0.01)  # the Sleeve's, not 0.015


# --- the shell builds the Sleeve ----------------------------------------------------------


async def test_the_guard_builds_a_fixed_sleeve_from_the_ledger_and_the_bot_spec(db: Database):
    two = await seed_bot(db, "bot-2", capital=10_000.0)
    outcome = await RiskGuard(db, LIMITS).evaluate(
        OrderIntent("BTC/USD", Side.BUY, notional=50_000.0),
        BTC,
        price=60_000.0,
        account=AccountState(equity=100_000.0),
        now=NOW,
        realized_vol=0.02,
        bot_id=two,
    )
    assert outcome.executable().notional == pytest.approx(500.0)


async def test_bot_one_through_the_guard_matches_the_account_alone(db: Database):
    one = await seed_bot(db)
    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.05)
    await BotPositionsRepo(db).upsert(bot_id=one, symbol="BTC/USD", qty=0.05)
    guard = RiskGuard(db, LIMITS)
    account = AccountState(equity=100_000.0, positions={"BTC/USD": 0.05})
    for intent in (
        OrderIntent("BTC/USD", Side.BUY, notional=50_000.0),
        OrderIntent("BTC/USD", Side.SELL, qty=0.05),
        OrderIntent("BTC/USD", Side.SELL, qty=0.01),
    ):
        kwargs = {"price": 60_000.0, "account": account, "now": NOW, "realized_vol": 0.02}
        before = await guard.evaluate(intent, BTC, **kwargs)
        after = await guard.evaluate(intent, BTC, bot_id=one, **kwargs)
        assert after.verdict_dicts == before.verdict_dicts
        assert after.intent == before.intent


async def test_a_bot_with_no_version_fails_closed(db: Database):
    await db.execute("INSERT INTO bots (name) VALUES ('ghost')")
    ghost = (await db.fetch_one("SELECT id FROM bots WHERE name = 'ghost'"))["id"]
    outcome = await RiskGuard(db, LIMITS).evaluate(
        OrderIntent("BTC/USD", Side.BUY, notional=500.0),
        BTC,
        price=60_000.0,
        account=AccountState(equity=100_000.0),
        now=NOW,
        bot_id=ghost,
    )
    assert denied_by(outcome) == "kill_switch"


async def test_a_bot_pause_through_the_store_blocks_only_that_bot(db: Database):
    from trader.risk.breakers import BreakerStore

    one = await seed_bot(db)
    two = await seed_bot(db, "bot-2", capital=10_000.0)
    await BreakerStore(db, bot_id=two).pause("bot maintenance")
    guard = RiskGuard(db, LIMITS)
    kwargs = {"price": 60_000.0, "account": AccountState(equity=100_000.0), "now": NOW}
    intent = OrderIntent("BTC/USD", Side.BUY, notional=300.0)
    assert denied_by(await guard.evaluate(intent, BTC, bot_id=two, **kwargs)) == "kill_switch"
    assert (await guard.evaluate(intent, BTC, bot_id=one, **kwargs)).approved
    assert not (await BreakerStore(db).load()).trading_paused  # the account is not paused


async def test_a_fixed_sleeve_s_breaker_trips_on_its_own_equity(db: Database):
    from trader.risk.breakers import DAILY_LOSS, BreakerStore

    two = await seed_bot(db, "bot-2", capital=10_000.0)
    guard = RiskGuard(db, replace(LIMITS, daily_loss_limit_frac=0.03))
    kwargs = {"account": AccountState(equity=100_000.0), "now": NOW, "bot_id": two}
    intent = OrderIntent("BTC/USD", Side.BUY, notional=300.0)
    assert (await guard.evaluate(intent, BTC, price=60_000.0, **kwargs)).approved
    # A closed trade lost 600: the Sleeve is down 6% on the day.
    await BotPositionsRepo(db).upsert(bot_id=two, symbol="ETH/USD", qty=0.0, realized_pnl=-600.0)
    outcome = await guard.evaluate(intent, BTC, price=60_000.0, **kwargs)
    assert denied_by(outcome) == "breaker"
    assert (await BreakerStore(db, bot_id=two).load()).halted_reason == DAILY_LOSS
    assert (await BreakerStore(db).load()).halted_reason is None  # not an Account halt


def test_a_fixed_sleeve_s_equity_is_capital_plus_realized_plus_marked_pnl():
    from trader.risk.sleeves import sleeve_equity

    rows = [
        {"symbol": "BTC/USD", "qty": 0.1, "avg_entry_price": 60_000.0, "realized_pnl": -50.0},
        {"symbol": "SPY", "qty": -2.0, "avg_entry_price": 600.0, "realized_pnl": 25.0},
        {"symbol": "ETH/USD", "qty": 1.0, "avg_entry_price": 2_000.0, "realized_pnl": 0.0},
    ]
    # BTC marked down 600, the SPY short up 20, ETH unmarked (held at its entry).
    equity = sleeve_equity(10_000.0, rows, {"BTC/USD": 54_000.0, "SPY": 590.0})
    assert equity == pytest.approx(10_000.0 - 50.0 + 25.0 - 600.0 + 20.0)


async def test_in_flight_and_stop_cooldown_are_per_bot(db: Database):
    from tests.test_loop import build_runner

    one = await seed_bot(db)
    two = await seed_bot(db, "bot-2", capital=10_000.0)
    await db.execute(
        "INSERT INTO orders (client_order_id, symbol, asset_class, side, qty, status, bot_id)"
        " VALUES ('open-2', 'BTC/USD', 'crypto', 'buy', 1, 'submitted', %s)",
        (two,),
    )
    assert await build_runner(db, [], bot_id=two)._order_in_flight("BTC/USD")
    assert not await build_runner(db, [], bot_id=one)._order_in_flight("BTC/USD")

    await db.execute(
        "INSERT INTO decisions (ts, trigger, status, symbol, bot_id)"
        " VALUES (%s, 'stop_loss', 'executed', 'BTC/USD', %s)",
        (NOW - timedelta(minutes=1), two),
    )
    guard = RiskGuard(db, LIMITS)
    kwargs = {"price": 60_000.0, "account": AccountState(equity=100_000.0), "now": NOW}
    intent = OrderIntent("BTC/USD", Side.BUY, notional=300.0)
    assert denied_by(await guard.evaluate(intent, BTC, bot_id=two, **kwargs)) == "stop_cooldown"
    assert (await guard.evaluate(intent, BTC, bot_id=one, **kwargs)).approved


async def test_trades_today_count_per_bot(db: Database):
    from trader.risk.sleeves import count_trades_today

    one = await seed_bot(db)
    two = await seed_bot(db, "bot-2", capital=10_000.0)
    for n, bot in enumerate((one, two, two)):
        await db.execute(
            "INSERT INTO orders (client_order_id, symbol, asset_class, side, qty, bot_id,"
            " created_at) VALUES (%s, 'BTC/USD', 'crypto', 'buy', 1, %s, %s)",
            (f"o-{n}", bot, NOW),
        )
    assert await count_trades_today(db, BTC, NOW) == 3
    assert await count_trades_today(db, BTC, NOW, bot_id=one) == 1
    assert await count_trades_today(db, BTC, NOW, bot_id=two) == 2


# --- the migration ------------------------------------------------------------------------


def test_the_migration_backfills_bot_one_s_sleeve_and_scopes_risk_state():
    import psycopg

    from trader.store.migrate import MIGRATIONS_DIR, run_migrations

    test_url = os.environ["TRADER_DATABASE_URL"]
    scratch_url = test_url + "_sleeves_backfill"
    name = scratch_url.rsplit("/", 1)[1]
    with psycopg.connect(test_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')

    sleeves_migration = next(p.name for p in sorted(MIGRATIONS_DIR.glob("*_sleeves.sql")))

    async def run() -> dict:
        db = Database(scratch_url, min_size=1, max_size=1)
        await db.open()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    if path.name < sleeves_migration:
                        shutil.copy(path, Path(tmp) / path.name)
                await run_migrations(db, Path(tmp))
            await db.execute(
                "INSERT INTO positions (symbol, asset_class, qty, avg_entry_price)"
                " VALUES ('ETH/USD', 'crypto', 0.25, 2000)"
            )
            await db.execute("UPDATE risk_state SET trading_paused = true, pause_reason = 'x'")
            await run_migrations(db)
            return {
                "sleeves": await db.fetch_all(
                    "SELECT bot_id, symbol, qty::float AS qty,"
                    " avg_entry_price::float AS avg FROM bot_positions"
                ),
                "risk": await db.fetch_all(
                    "SELECT scope, trading_paused FROM risk_state ORDER BY scope"
                ),
            }
        finally:
            await db.close()

    try:
        out = asyncio.run(run())
    finally:
        with psycopg.connect(test_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')

    assert out["sleeves"] == [{"bot_id": 1, "symbol": "ETH/USD", "qty": 0.25, "avg": 2000.0}]
    # The old singleton is the account scope, state kept; Bot #1 gets its own, clear.
    assert out["risk"] == [
        {"scope": "account", "trading_paused": True},
        {"scope": "bot:1", "trading_paused": False},
    ]


# --- every door shows the Sleeve ----------------------------------------------------------


async def test_the_bot_read_shows_its_sleeve(db: Database):
    from trader.surfaces.services import build_registry

    bot = await seed_bot(db, "bot-2", capital=10_000.0)
    await BotPositionsRepo(db).upsert(
        bot_id=bot, symbol="BTC/USD", qty=0.1, avg_entry_price=60_000.0
    )
    reg = build_registry(db)
    shown = await reg.get("bot").handler(name="bot-2")
    assert shown["sleeve"]["capital_usd"] == 10_000.0
    assert shown["sleeve"]["positions"] == [
        {"symbol": "BTC/USD", "qty": 0.1, "avg_entry_price": 60_000.0, "realized_pnl": 0.0}
    ]
    assert shown["sleeve"]["risk"]["trading_paused"] is False
    listed = await reg.get("bots").handler()
    assert listed[0]["sleeve"] == shown["sleeve"]

    await reg.get("pause").handler(reason="look", bot="bot-2")
    paused = await reg.get("bot").handler(name="bot-2")
    assert paused["sleeve"]["risk"]["trading_paused"] is True
    assert (await reg.get("risk_status").handler())["trading_paused"] is False
    await reg.get("resume").handler(bot="bot-2")
    assert (await reg.get("bot").handler(name="bot-2"))["sleeve"]["risk"]["trading_paused"] is False


def test_a_bot_pause_reaches_every_door(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    boot = settings(loop_symbols="BTC/USD")
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(boot)))

    def paused(tc: TestClient) -> bool:
        return tc.get(f"/bots/{BOT_ONE}").json()["sleeve"]["risk"]["trading_paused"]

    with TestClient(create_app(boot)) as tc:  # boot seeds Bot #1
        assert tc.post("/risk/pause", params={"bot": BOT_ONE}).json()["bot"] == BOT_ONE
        assert paused(tc) and not tc.get("/risk/status").json()["trading_paused"]
        tc.post("/risk/resume", params={"bot": BOT_ONE})
        assert not paused(tc)
        assert tc.post("/risk/pause", params={"bot": "nope"}).status_code == 404

        printed = CliRunner().invoke(cli.app, ["risk", "pause", "--bot", BOT_ONE])
        assert printed.exit_code == 0, printed.output
        assert paused(tc)
        CliRunner().invoke(cli.app, ["risk", "resume", "--bot", BOT_ONE])
        assert not paused(tc)

        async def via_mcp() -> None:
            db = Database(min_size=1, max_size=1)
            await db.open()
            try:
                async with Client(build_mcp(build_registry(db))) as mcp_client:
                    out = await mcp_client.call_tool("pause", {"bot": BOT_ONE})
                assert not out.is_error, out.content
            finally:
                await db.close()

        asyncio.run(via_mcp())
        assert paused(tc)
