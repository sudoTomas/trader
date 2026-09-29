"""The decide loop — triggers, context assembly, decider validation, and the shadow-mode
runner end to end. The LLM and market data are stubbed; the store is the real one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from trader.core.symbols import AssetClass, default_registry, make_symbol
from trader.core.types import Action, Bar
from trader.llm.base import Completion, LLMError, Usage
from trader.loop.context import ContextBuilder, compute_indicators
from trader.loop.decider import DECISION_SCHEMA, Decider, DeciderError, DecisionOutput
from trader.loop.journal import DENIED, ERROR, SHADOW, SUPPRESSED, Journal
from trader.loop.runner import LoopRunner
from trader.loop.scheduler import (
    HEARTBEAT,
    POSITION_RISK,
    PRICE_MOVE,
    VOL_SPIKE,
    Mark,
    SignalScheduler,
    TriggerConfig,
)
from trader.risk.guard import RiskGuard
from trader.risk.limits import RiskLimits
from trader.store.db import Database

BTC = default_registry().get("BTC/USD")
SPY = make_symbol("SPY", AssetClass.US_EQUITY)
NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)  # Wednesday 11:00 ET
SATURDAY = datetime(2026, 8, 15, 15, 0, tzinfo=UTC)

LIMITS = RiskLimits(max_position_frac=0.05, approval_notional_usd=100_000.0)


def bars(closes: list[float], symbol: str = "BTC/USD") -> list[Bar]:
    return [
        Bar(symbol, "1h", NOW - timedelta(hours=len(closes) - i), c, c * 1.01, c * 0.99, c, 10.0)
        for i, c in enumerate(closes)
    ]


# --- scheduler --------------------------------------------------------------


def test_heartbeat_fires_then_respects_its_cadence():
    scheduler = SignalScheduler([BTC], TriggerConfig(price_move_pct=9.0))
    first = scheduler.evaluate(NOW, {"BTC/USD": Mark(price=60_000)})
    assert [t.type for t in first] == [HEARTBEAT]

    scheduler.mark_dispatched(first, NOW)
    soon = scheduler.evaluate(NOW + timedelta(minutes=5), {"BTC/USD": Mark(price=60_000)})
    assert soon == []
    later = scheduler.evaluate(NOW + timedelta(minutes=20), {"BTC/USD": Mark(price=60_000)})
    assert [t.type for t in later] == [HEARTBEAT]


def test_undispatched_triggers_refire_next_tick():
    """THE cooldown discipline: a trigger suppressed by an in-flight run must come back."""
    scheduler = SignalScheduler([BTC])
    scheduler.mark_dispatched(scheduler.evaluate(NOW, {"BTC/USD": Mark(price=60_000)}), NOW)

    later = NOW + timedelta(minutes=1)
    moved = scheduler.evaluate(later, {"BTC/USD": Mark(price=63_000)})  # +5%
    assert PRICE_MOVE in [t.type for t in moved]

    # Simulate "the loop was busy": evaluate again WITHOUT marking dispatched.
    again = scheduler.evaluate(later + timedelta(seconds=30), {"BTC/USD": Mark(price=63_000)})
    assert PRICE_MOVE in [t.type for t in again]

    # Once dispatched, the cooldown finally applies.
    scheduler.mark_dispatched(again, later)
    quiet = scheduler.evaluate(later + timedelta(minutes=1), {"BTC/USD": Mark(price=63_000)})
    assert PRICE_MOVE not in [t.type for t in quiet]


def test_cooldowns_are_scoped_per_symbol():
    eth = default_registry().get("ETH/USD")
    scheduler = SignalScheduler([BTC, eth])
    start = {"BTC/USD": Mark(price=60_000), "ETH/USD": Mark(price=3_000)}
    scheduler.mark_dispatched(scheduler.evaluate(NOW, start), NOW)

    later = NOW + timedelta(minutes=1)
    btc_moved = scheduler.evaluate(later, {"BTC/USD": Mark(price=63_000), **start})
    scheduler.mark_dispatched(btc_moved, later)

    # ETH's own move must not be suppressed by BTC's dispatch.
    eth_moved = scheduler.evaluate(later + timedelta(minutes=1), {"ETH/USD": Mark(price=3_200)})
    assert [(t.type, t.symbol) for t in eth_moved] == [(PRICE_MOVE, "ETH/USD")]


def test_vol_spike_and_position_risk_fire():
    scheduler = SignalScheduler([BTC], TriggerConfig(price_move_pct=9.0))
    for i in range(3):
        scheduler.evaluate(NOW + timedelta(minutes=i), {"BTC/USD": Mark(60_000, realized_vol=0.01)})
    spiked = scheduler.evaluate(
        NOW + timedelta(minutes=4), {"BTC/USD": Mark(60_000, realized_vol=0.05)}
    )
    assert VOL_SPIKE in [t.type for t in spiked]

    risky = scheduler.evaluate(
        NOW + timedelta(hours=2), {"BTC/USD": Mark(60_000, unrealized_pnl_frac=-0.08)}
    )
    assert POSITION_RISK in [t.type for t in risky]


def test_closed_venue_produces_no_triggers():
    scheduler = SignalScheduler([SPY])
    assert scheduler.evaluate(SATURDAY, {"SPY": Mark(price=600)}) == []
    assert [t.type for t in scheduler.evaluate(NOW, {"SPY": Mark(price=600)})] == [HEARTBEAT]


# --- context ----------------------------------------------------------------


class StubMarketData:
    name = "stub"

    def __init__(self, series: list[float] | None = None, *, fail: bool = False) -> None:
        self._series = series if series is not None else [100.0 + i for i in range(60)]
        self._fail = fail
        self.bar_calls = 0

    async def get_bars(self, symbol, interval, lookback):
        self.bar_calls += 1
        if self._fail:
            from trader.data.base import MarketDataError

            raise MarketDataError("provider down")
        return bars(self._series, symbol.ticker)

    async def get_latest_quote(self, symbol):
        from trader.core.types import Quote

        if self._fail:
            from trader.data.base import MarketDataError

            raise MarketDataError("provider down")
        return Quote(symbol.ticker, NOW, self._series[-1], self._series[-1])


async def test_context_computes_indicators_locally_and_never_calls_a_tool():
    data = StubMarketData()
    ctx = await ContextBuilder(data).build(BTC, now=NOW, triggers=[], equity=100_000.0)

    assert data.bar_calls == 1  # one fetch, no per-indicator network hops
    assert ctx.price == pytest.approx(159.0)
    indicators = ctx.indicators["1h"]
    assert indicators.rsi_14 is not None and indicators.sma_20 is not None
    prompt = ctx.to_prompt()
    assert "rsi_14" in prompt and "BTC/USD" in prompt


async def test_context_degrades_when_data_is_unavailable():
    ctx = await ContextBuilder(StubMarketData(fail=True)).build(BTC, now=NOW, triggers=[])
    assert ctx.bars["1h"] == []
    assert ctx.price == 0.0  # the runner treats this as "decide nothing"


def test_indicators_are_independently_optional():
    short = compute_indicators(bars([1.0, 2.0, 3.0]))
    assert short.rsi_14 is None and short.sma_20 is None  # too few bars
    assert short.ema_12 is None
    full = compute_indicators(bars([100.0 + i for i in range(40)]))
    assert full.rsi_14 is not None and full.realized_vol_20 is not None


async def test_price_prefers_the_live_quote_over_the_last_bar_close():
    """The mark must not inherit bar staleness (a completed 1h bar is up to an hour
    old; with sip's delayed end, more) — price is quote-first, bars are the fallback."""

    class QuoteAheadData(StubMarketData):
        async def get_latest_quote(self, symbol):
            from trader.core.types import Quote

            return Quote(symbol.ticker, NOW, 170.0, 171.0)  # fresher than last close 159

    ctx = await context_for(QuoteAheadData())
    assert ctx.price == 170.5  # quote mid, not bars[-1].close
    assert ctx.bars["1h"]  # bars still present for indicators


async def test_price_falls_back_to_last_bar_close_when_the_quote_fails():
    class QuoteDownData(StubMarketData):
        async def get_latest_quote(self, symbol):
            from trader.data.base import MarketDataError

            raise MarketDataError("quote feed down")

    ctx = await context_for(QuoteDownData())
    assert ctx.price == 159.0  # last close of the default rising series


# --- decider ----------------------------------------------------------------


class StubLLM:
    """Stands in for LLMRegistry.complete with a scripted reply sequence."""

    def __init__(self, replies: list[str | Exception]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    async def complete(self, role, messages, *, schema=None, max_tokens=1024, temperature=0.2):
        self.calls.append(messages)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Completion(
            text=reply,
            usage=Usage(prompt_tokens=100, completion_tokens=20),
            cost_usd=0.0,
            provider="nemotron",
            model="nemotron-3-super",
        )


async def context_for(data: StubMarketData | None = None):
    return await ContextBuilder(data or StubMarketData()).build(
        BTC, now=NOW, triggers=[], equity=100_000.0
    )


def test_decision_schema_is_generated_from_the_model():
    assert DECISION_SCHEMA["properties"]["action"]["enum"] == ["hold", "open", "close"]
    with pytest.raises(ValueError, match="requires a side and a positive notional_usd"):
        DecisionOutput(action="open", conviction=0.9, rationale="x").to_decision("BTC/USD")
    with pytest.raises(ValueError, match="positive notional_usd"):
        DecisionOutput(
            action="open", conviction=0.9, rationale="x", side="buy", notional_usd=0
        ).to_decision("BTC/USD")


def test_wire_schema_requires_every_key_but_validation_stays_lenient():
    """Desk gate finding: under guided decoding the decider omitted side/notional on
    opens (schema-invalid twice, 5×/336 steps with the analyst lane). The WIRE schema
    now forces every key to be emitted — omission becomes ungrammatical, holds write
    nulls — while the pydantic validator stays lenient for stubs and non-guided paths
    (see docs/lessons.md#guided-decoding-omitted-keys)."""
    assert set(DECISION_SCHEMA["required"]) == set(DECISION_SCHEMA["properties"])
    lenient = DecisionOutput.model_validate_json(
        '{"action":"hold","conviction":0.3,"rationale":"nothing doing"}'
    )
    assert lenient.side is None and lenient.notional_usd is None


def test_decide_prompt_states_the_open_close_field_requirement():
    from trader.loop.decider import SYSTEM_PROMPT

    assert "`side`" in SYSTEM_PROMPT  # the conditional requirement is spelled out
    assert "`notional_usd`" in SYSTEM_PROMPT


def test_hold_with_zero_notional_needs_no_retry():
    """Live models answer notional_usd=0 for a hold; rejecting that burned an extra
    inference call on the most common decision path."""
    decision = DecisionOutput(
        action="hold", conviction=0.0, rationale="no signal", notional_usd=0
    ).to_decision("BTC/USD")
    assert decision.action is Action.HOLD and decision.intent is None


async def test_decider_parses_a_valid_reply():
    llm = StubLLM(['{"action":"hold","conviction":0.4,"rationale":"rsi neutral"}'])
    result = await Decider(llm).decide(await context_for())
    assert result.decision.action is Action.HOLD
    assert result.attempts == 1
    assert result.decision.intent is None


async def test_decider_retries_once_with_the_validation_errors():
    llm = StubLLM(
        [
            '{"action":"open","conviction":2.5,"rationale":"too confident"}',  # conviction > 1
            '{"action":"open","conviction":0.8,"rationale":"ok","side":"buy","notional_usd":500}',
        ]
    )
    result = await Decider(llm).decide(await context_for())
    assert result.attempts == 2
    assert result.decision.intent.notional == 500.0

    # The retry must actually carry the validation errors back to the model.
    retry_prompt = llm.calls[1][-1]["content"]
    assert "failed validation" in retry_prompt and "conviction" in retry_prompt


async def test_two_bad_replies_is_a_non_transient_decider_error():
    llm = StubLLM(["not json", "still not json"])
    with pytest.raises(DeciderError) as excinfo:
        await Decider(llm).decide(await context_for())
    assert excinfo.value.transient is False


async def test_llm_outage_is_classified_transient():
    llm = StubLLM([LLMError("endpoint down")])
    with pytest.raises(DeciderError) as excinfo:
        await Decider(llm).decide(await context_for())
    assert excinfo.value.transient is True  # infra: back off, don't arm a crash loop


# --- runner (shadow mode) ---------------------------------------------------


def build_runner(db: Database, replies: list[str | Exception], **kwargs) -> LoopRunner:
    return LoopRunner(
        db=db,
        market_data=kwargs.pop("market_data", StubMarketData()),
        llm=StubLLM(replies),
        guard=kwargs.pop("guard", RiskGuard(db, LIMITS)),
        symbols=kwargs.pop("symbols", [BTC]),
        shadow=kwargs.pop("shadow", True),
        equity=100_000.0,
        **kwargs,
    )


HOLD = '{"action":"hold","conviction":0.3,"rationale":"nothing doing"}'
OPEN = '{"action":"open","conviction":0.8,"rationale":"momentum","side":"buy","notional_usd":500}'


async def test_shadow_wakeup_journals_everything_and_submits_nothing(db: Database):
    runner = build_runner(db, [OPEN])
    results = await runner.tick(NOW)

    assert [r.status for r in results] == [SHADOW]
    row = await Journal(db).get(results[0].decision_id)
    assert row["trigger"] == HEARTBEAT
    assert row["status"] == SHADOW
    assert row["prompt"] and row["raw_output"]  # replay-ready
    assert row["decision"]["action"] == "open"
    assert [v["check"] for v in row["risk_verdicts"]][0] == "input_sanity"
    assert row["cost_usd"] is not None and row["latency_ms"] is not None

    # Shadow parks no approvals and creates no orders.
    assert await db.fetch_all("SELECT * FROM approvals") == []
    assert await db.fetch_all("SELECT * FROM orders") == []


async def test_hold_decisions_are_journaled_without_a_risk_pass(db: Database):
    results = await build_runner(db, [HOLD]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["decision"]["action"] == "hold"
    assert row["risk_verdicts"] is None  # nothing to size, nothing to check


async def test_risk_denial_is_journaled_as_denied(db: Database):
    from trader.risk.breakers import BreakerStore

    await BreakerStore(db).pause("test")
    results = await build_runner(db, [OPEN]).tick(NOW)
    assert [r.status for r in results] == [DENIED]
    row = await Journal(db).get(results[0].decision_id)
    assert row["status"] == DENIED
    assert row["risk_verdicts"][-1]["check"] == "kill_switch"


async def test_unusable_market_data_journals_a_suppressed_wakeup(db: Database):
    runner = build_runner(db, [], market_data=StubMarketData(fail=True))
    results = await runner.tick(NOW)
    assert [r.status for r in results] == [SUPPRESSED]
    assert (await Journal(db).get(results[0].decision_id))["status"] == SUPPRESSED


async def test_bad_model_output_journals_an_error_without_crashing(db: Database):
    results = await build_runner(db, ["garbage", "still garbage"]).tick(NOW)
    assert [r.status for r in results] == [ERROR]
    assert (await Journal(db).get(results[0].decision_id))["status"] == ERROR


async def test_transient_llm_failure_propagates_for_backoff(db: Database):
    runner = build_runner(db, [LLMError("spark down")])
    with pytest.raises(DeciderError) as excinfo:
        await runner.tick(NOW)
    assert excinfo.value.transient
    # Still journaled: the operator can see the loop tried.
    assert (await Journal(db).recent(1))[0]["status"] == ERROR


async def test_force_wake_ignores_cooldowns(db: Database):
    runner = build_runner(db, [HOLD, HOLD])
    assert len(await runner.tick(NOW)) == 1
    assert await runner.tick(NOW + timedelta(seconds=5)) == []  # heartbeat cadence not due
    forced = await runner.tick(NOW + timedelta(seconds=10), force=True)
    assert [r.triggers for r in forced] == [[HEARTBEAT]]


async def test_runner_is_single_flight(db: Database):
    import asyncio

    runner = build_runner(db, [HOLD, HOLD])
    async with runner._lock:  # simulate a wakeup already in flight
        assert await runner.tick(NOW) == []
    # The trigger was never dispatched, so it is still available.
    assert len(await runner.tick(NOW)) == 1
    assert isinstance(runner._lock, asyncio.Lock)


async def test_shadow_journal_is_replayable(db: Database):
    """The journal row alone must be enough to re-run the decision offline."""
    results = await build_runner(db, [OPEN]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    replayed = DecisionOutput.model_validate_json(row["raw_output"]).to_decision(row["symbol"])
    assert replayed.action is Action.OPEN
    assert json.loads(json.dumps(row["decision"]))["conviction"] == 0.8


# --- account equity (#13) ---------------------------------------------------


class StubAccount:
    """Broker stand-in: only ``get_account`` — the runner's equity source."""

    def __init__(self, equity: float = 100_025.08, *, fail: bool = False) -> None:
        self.equity = equity
        self.fail = fail
        self.calls = 0

    async def get_account(self):
        from trader.brokers.base import AccountSnapshot, BrokerError

        self.calls += 1
        if self.fail:
            raise BrokerError("alpaca down")
        return AccountSnapshot(
            equity=self.equity,
            cash=self.equity,
            buying_power=self.equity,
            non_marginable_buying_power=self.equity,
            status="ACTIVE",
            tradable=True,
        )


async def test_broker_equity_sizes_the_prompt_not_shadow_equity(db: Database):
    """A live account snapshot replaces the shadow_equity constant in the prompt."""
    from trader.risk.breakers import BreakerStore

    broker = StubAccount(equity=100_025.08)
    runner = build_runner(db, [OPEN])
    runner.broker = broker
    results = await runner.tick(NOW)

    assert broker.calls == 1
    row = await Journal(db).get(results[0].decision_id)
    assert "Account equity: 100,025.08 USD" in row["prompt"]
    assert "Account equity: 100,000.00 USD" not in row["prompt"]
    state = await BreakerStore(db).load()
    assert state.day_start_equity == pytest.approx(100_025.08)
    assert state.week_high_equity == pytest.approx(100_025.08)


async def test_shadow_build_without_a_broker_keeps_shadow_equity(db: Database):
    runner = build_runner(db, [HOLD])
    results = await runner.tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert "Account equity: 100,000.00 USD" in row["prompt"]
    assert await db.fetch_all("SELECT id FROM decisions WHERE trigger = 'equity_fallback'") == []


async def test_alpaca_down_keeps_last_good_equity_and_journals_it(db: Database):
    """A venue blip must not silently resize risk back to the shadow constant."""
    from trader.loop.runner import EQUITY_CACHE_SECONDS

    broker = StubAccount(equity=100_025.08)
    runner = build_runner(db, [HOLD, HOLD])
    runner.broker = broker
    await runner.tick(NOW)
    broker.fail = True
    later = NOW + timedelta(seconds=EQUITY_CACHE_SECONDS + 5)
    results = await runner.tick(later, force=True)

    row = await Journal(db).get(results[0].decision_id)
    assert "Account equity: 100,025.08 USD" in row["prompt"]
    assert "Account equity: 100,000.00 USD" not in row["prompt"]
    fallbacks = await db.fetch_all(
        "SELECT raw_output FROM decisions WHERE trigger = 'equity_fallback'"
    )
    assert len(fallbacks) == 1
    assert "last_good" in fallbacks[0]["raw_output"]
    assert "100025.08" in fallbacks[0]["raw_output"]
    # The fallback row is an audit of the blip, not an input to the next decide.
    assert "equity_fallback" not in row["prompt"]


async def test_alpaca_down_with_no_last_good_journals_the_constant(db: Database):
    """No cached equity: the constant may be used only when the journal says so."""
    broker = StubAccount(fail=True)
    runner = build_runner(db, [HOLD, HOLD])
    runner.broker = broker
    await runner.tick(NOW)
    await runner.tick(NOW + timedelta(minutes=20), force=True)

    assert broker.calls == 2  # a failure is not cached; the next tick tries again
    fallbacks = await db.fetch_all(
        "SELECT raw_output FROM decisions WHERE trigger = 'equity_fallback'"
    )
    assert len(fallbacks) == 1  # one row per outage, not one per tick
    assert "shadow_equity_constant" in fallbacks[0]["raw_output"]


async def test_broker_equity_is_cached_for_a_few_seconds(db: Database):
    from trader.loop.runner import EQUITY_CACHE_SECONDS

    broker = StubAccount(equity=50_000.0)
    runner = build_runner(db, [HOLD])
    runner.broker = broker
    await runner.tick(NOW)
    await runner.tick(NOW + timedelta(seconds=EQUITY_CACHE_SECONDS - 1))
    assert broker.calls == 1
    await runner.tick(NOW + timedelta(seconds=EQUITY_CACHE_SECONDS + 5))
    assert broker.calls == 2


def _utc_midnight_pair() -> tuple[datetime, datetime]:
    """Two aware timestamps on opposite sides of the current UTC midnight."""
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    if now.hour >= 12:
        before = now.replace(hour=22)
        after = (before + timedelta(days=1)).replace(hour=1)
    else:
        after = now.replace(hour=1)
        before = (after - timedelta(days=1)).replace(hour=22)
    return before, after


async def test_holds_across_a_utc_day_roll_the_breaker_session(db: Database):
    """A run of holds must still roll day_session and re-baseline daily equity."""
    from trader.risk.breakers import BreakerStore

    before, after = _utc_midnight_pair()
    broker = StubAccount(equity=100_000.0)
    runner = build_runner(db, [HOLD, HOLD])
    runner.broker = broker

    await runner.tick(before, force=True)
    state = await BreakerStore(db).load()
    assert state.day_session == before.date()
    assert state.day_start_equity == pytest.approx(100_000.0)

    broker.equity = 100_025.0
    await runner.tick(after, force=True)
    state = await BreakerStore(db).load()
    assert state.day_session == after.date()
    assert state.day_start_equity == pytest.approx(100_025.0)
    rows = await db.fetch_all(
        "SELECT decision, risk_verdicts FROM decisions WHERE trigger != 'equity_fallback'"
    )
    assert len(rows) == 2
    assert all(row["decision"]["action"] == "hold" for row in rows)
    assert all(row["risk_verdicts"] is None for row in rows)


async def test_a_hold_tick_trips_daily_loss_when_equity_bleeds(db: Database):
    from trader.loop.runner import EQUITY_CACHE_SECONDS
    from trader.risk.breakers import DAILY_LOSS, BreakerStore

    now = datetime.now(tz=UTC)
    broker = StubAccount(equity=100_000.0)
    runner = build_runner(db, [HOLD, HOLD])
    runner.broker = broker
    await runner.tick(now, force=True)
    broker.equity = 96_000.0  # -4%, outside the 3% daily-loss limit
    await runner.tick(now + timedelta(seconds=EQUITY_CACHE_SECONDS + 5), force=True)
    state = await BreakerStore(db).load()
    assert state.halted_reason == DAILY_LOSS
    assert state.day_start_equity == pytest.approx(100_000.0)


# --- trades today (#12) -----------------------------------------------------


def _capped_guard(db: Database, max_trades_per_day: int) -> RiskGuard:
    return RiskGuard(
        db,
        RiskLimits(
            max_position_frac=0.5,
            approval_notional_usd=100_000.0,
            max_trades_per_day=max_trades_per_day,
        ),
    )


async def test_daily_trade_cap_counts_orders_placed_today(db: Database):
    from trader.store.repos import OrdersRepo

    await OrdersRepo(db).insert(
        client_order_id="cap-today",
        symbol="BTC/USD",
        asset_class="crypto",
        side="buy",
        notional=25,
    )
    now = datetime.now(tz=UTC)
    runner = build_runner(db, [OPEN], guard=_capped_guard(db, 1))
    results = await runner.tick(now, force=True)

    assert results[0].status == DENIED
    row = await Journal(db).get(results[0].decision_id)
    assert row["risk_verdicts"][-1]["check"] == "max_trades_per_day"


async def test_orders_from_the_previous_session_do_not_spend_the_cap(db: Database):
    now = datetime.now(tz=UTC)
    await db.execute(
        """
        INSERT INTO orders (client_order_id, symbol, asset_class, side, notional, created_at)
        VALUES ('cap-yesterday', 'BTC/USD', 'crypto', 'buy', 25, %s)
        """,
        (now - timedelta(days=1),),
    )
    runner = build_runner(db, [OPEN], guard=_capped_guard(db, 1))
    results = await runner.tick(now, force=True)
    assert results[0].status == SHADOW


# --- decision_id on live orders (#14) ---------------------------------------


async def test_live_order_carries_the_decision_id(db: Database):
    from tests.test_executor import FakeAlpaca
    from trader.loop.executor import Executor
    from trader.loop.journal import EXECUTED

    broker = FakeAlpaca()
    runner = build_runner(db, [OPEN], shadow=False, executor=Executor(db, broker))
    results = await runner.tick(datetime.now(tz=UTC), force=True)

    assert results[0].status == EXECUTED
    assert len(broker.submitted) == 1
    order = await db.fetch_one("SELECT decision_id, status FROM orders")
    assert order["status"] == "submitted"
    assert order["decision_id"] == results[0].decision_id
    row = await Journal(db).get(results[0].decision_id)
    assert row["status"] == EXECUTED
    assert row["decision"]["action"] == "open"
    assert row["risk_verdicts"]


async def test_rejected_submit_still_links_the_order_to_its_decision(db: Database):
    from tests.test_executor import FakeAlpaca
    from trader.loop.executor import Executor
    from trader.loop.journal import ERROR

    broker = FakeAlpaca(fail="error")
    runner = build_runner(db, [OPEN], shadow=False, executor=Executor(db, broker))
    results = await runner.tick(datetime.now(tz=UTC), force=True)

    assert results[0].status == ERROR
    order = await db.fetch_one("SELECT decision_id FROM orders")
    assert order["decision_id"] == results[0].decision_id
    assert (await Journal(db).get(results[0].decision_id))["status"] == ERROR


# --- marks: vol and unrealized pnl (#15) ------------------------------------


def _capturing_scheduler():
    seen: dict[str, Mark] = {}
    scheduler = SignalScheduler([BTC], TriggerConfig(price_move_pct=9.0))

    def capture(now, marks):
        seen.update(marks)
        return SignalScheduler.evaluate(scheduler, now, marks)

    scheduler.evaluate = capture  # type: ignore[method-assign]
    return scheduler, seen


async def test_marks_carry_realized_vol_and_unrealized_pnl(db: Database):
    from trader.data.indicators import realized_vol
    from trader.store.repos import PositionsRepo

    series = [100.0 + i for i in range(60)]
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=1.0, avg_entry_price=150.0
    )
    scheduler, seen = _capturing_scheduler()
    runner = build_runner(db, [HOLD], market_data=StubMarketData(series), scheduler=scheduler)
    results = await runner.tick(NOW)

    mark = seen["BTC/USD"]
    assert mark.realized_vol == pytest.approx(realized_vol(bars(series), 20))
    assert mark.unrealized_pnl_frac == pytest.approx((159.0 - 150.0) / 150.0)
    assert POSITION_RISK in results[0].triggers


async def test_short_unrealized_pnl_is_signed_against_the_position(db: Database):
    from trader.store.repos import PositionsRepo

    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=-2.0, avg_entry_price=100.0
    )
    scheduler, seen = _capturing_scheduler()
    runner = build_runner(db, [HOLD], market_data=StubMarketData([110.0] * 30), scheduler=scheduler)
    results = await runner.tick(NOW)

    # Price above entry: a short is losing, so the fraction is negative.
    assert seen["BTC/USD"].unrealized_pnl_frac == pytest.approx(-0.10)
    assert POSITION_RISK in results[0].triggers


async def test_mark_vol_stays_empty_when_bars_are_unavailable(db: Database):
    class BarsDown(StubMarketData):
        async def get_bars(self, symbol, interval, lookback):
            from trader.data.base import MarketDataError

            raise MarketDataError("bars down")

    scheduler, seen = _capturing_scheduler()
    runner = build_runner(db, [HOLD], market_data=BarsDown(), scheduler=scheduler)
    results = await runner.tick(NOW)

    assert results[0].status == SHADOW  # the quote still decides
    assert seen["BTC/USD"].price == pytest.approx(159.0)
    assert seen["BTC/USD"].realized_vol is None
    assert seen["BTC/USD"].unrealized_pnl_frac is None


# --- sentiment cadence (#22) ------------------------------------------------


class RecordingSentiment:
    def __init__(self) -> None:
        self.polled: list[str] = []

    async def get_x_sentiment(self, symbol, **kwargs):
        self.polled.append(symbol.ticker)
        return {"status": "ok", "symbol": symbol.ticker}


async def test_sentiment_polls_held_symbols_only_every_three_hours(db: Database):
    """Unheld names are pure cost: the scheduler only reads sentiment for positions."""
    from trader.ops.config import Settings
    from trader.store.repos import PositionsRepo

    eth = default_registry().get("ETH/USD")
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=0.5, avg_entry_price=100.0
    )
    svc = RecordingSentiment()
    # force=True wakes every open symbol, held or not — six decides, two polls.
    runner = build_runner(db, [HOLD] * 6, symbols=[BTC, eth], sentiment=svc)

    # 3h cadence. The daily cap still bounds a shorter interval, and the wake
    # trigger default is untouched (the live book arms it at 0.8 via env).
    fields = Settings.model_fields
    assert runner.sentiment_poll_seconds == 10_800
    assert fields["sentiment_poll_seconds"].default == 10_800
    assert fields["sentiment_daily_call_cap"].default == 24
    assert fields["sentiment_trigger_enabled"].default is False
    assert fields["sentiment_swing_threshold"].default == 0.5

    now = datetime.now(tz=UTC)
    await runner.tick(now, force=True)
    assert svc.polled == ["BTC/USD"]
    await runner.tick(now + timedelta(hours=2), force=True)
    assert svc.polled == ["BTC/USD"]  # inside the 3h window
    await runner.tick(now + timedelta(hours=3), force=True)
    assert svc.polled == ["BTC/USD", "BTC/USD"]
