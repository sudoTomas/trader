"""Broker + executor — the first code that can place an order.

Covers the live-URL refusal, the no-bypass link, persist-before-submit recovery, fill
dedupe, drift detection, and the confirm gate. A FakeAlpaca stands in for the venue;
the store is real. The live-paper round trip needs operator credentials (see EAI-176).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from trader.brokers.alpaca import PAPER_REST, AlpacaBroker, _decode, _to_event
from trader.brokers.base import (
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    DuplicateOrder,
    OrderEvent,
)
from trader.core.symbols import AssetClass, default_registry, make_symbol
from trader.core.types import OrderIntent, Side
from trader.loop.executor import Executor, venue_order_params
from trader.risk.guard import AccountState, RiskDenied, RiskGuard
from trader.risk.limits import RiskLimits
from trader.store.db import Database

BTC = default_registry().get("BTC/USD")
SPY = make_symbol("SPY", AssetClass.US_EQUITY)
NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
LIMITS = RiskLimits(max_position_frac=0.05, approval_notional_usd=1_000_000.0)


# --- live gate --------------------------------------------------------------


def test_live_client_cannot_be_constructed_without_the_flag():
    with pytest.raises(BrokerError, match="refusing to construct a LIVE"):
        AlpacaBroker("k", "s", live=True, live_enabled=False)

    paper = AlpacaBroker("k", "s")
    assert paper.live is False
    assert str(paper._client.base_url).startswith(PAPER_REST)

    # Only an explicit operator opt-in permits it.
    assert AlpacaBroker("k", "s", live=True, live_enabled=True).live is True


def test_broker_requires_credentials():
    with pytest.raises(BrokerError, match="requires ALPACA_API_KEY_ID"):
        AlpacaBroker("", "")


# --- REST mapping -----------------------------------------------------------


def broker_with(handler) -> AlpacaBroker:
    return AlpacaBroker(
        "k",
        "s",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://p"),
    )


ACCOUNT = {
    "status": "ACTIVE",
    "equity": "100000.55",
    "cash": "50000",
    "buying_power": "200000",
    "non_marginable_buying_power": "50000",
    "trading_blocked": False,
    "account_blocked": False,
    "trade_suspended_by_user": False,
}


async def test_account_parses_strings_and_health_predicate():
    broker = broker_with(lambda r: httpx.Response(200, json=ACCOUNT))
    account = await broker.get_account()
    assert account.equity == 100000.55  # strings → floats
    assert account.healthy and await broker.is_healthy()

    blocked = broker_with(lambda r: httpx.Response(200, json={**ACCOUNT, "trading_blocked": True}))
    assert not (await blocked.get_account()).healthy
    assert not await blocked.is_healthy()


async def test_revoked_key_flips_health_without_raising():
    """Kill-test: credentials revoked mid-session must degrade, not explode."""
    broker = broker_with(lambda r: httpx.Response(401, json={"message": "forbidden"}))
    assert await broker.is_healthy() is False
    with pytest.raises(BrokerError, match="HTTP 401"):
        await broker.get_account()


async def test_positions_use_qty_available():
    rows = [
        {
            "symbol": "BTC/USD",
            "qty": "0.5",
            "qty_available": "0.3",  # 0.2 committed to an open order
            "avg_entry_price": "60000",
            "asset_class": "crypto",
        }
    ]
    broker = broker_with(lambda r: httpx.Response(200, json=rows))
    position = (await broker.get_positions())[0]
    assert position.qty == 0.5
    assert position.qty_available == 0.3  # closing on qty would oversell


async def test_duplicate_client_order_id_is_not_a_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"message":"client_order_id must be unique"}')

    with pytest.raises(DuplicateOrder):
        await broker_with(handler).submit_order(
            client_order_id="coid", symbol="BTC/USD", side="buy", qty=0.01
        )


async def test_submit_validates_size_and_id_length():
    broker = broker_with(lambda r: httpx.Response(200, json={"id": "x"}))
    with pytest.raises(BrokerError, match="exactly one of qty or notional"):
        await broker.submit_order(client_order_id="c", symbol="BTC/USD", side="buy")
    with pytest.raises(BrokerError, match="128-character"):
        await broker.submit_order(client_order_id="x" * 129, symbol="BTC/USD", side="buy", qty=1.0)


# --- stream decoding --------------------------------------------------------


def test_stream_frames_are_binary_json():
    assert _decode(b'{"stream":"trade_updates"}')["stream"] == "trade_updates"
    assert _decode('{"a":1}')["a"] == 1
    assert _decode(b"not json") == {}


def test_stream_event_mapping():
    event = _to_event(
        {
            "event": "partial_fill",
            "timestamp": "2026-08-12T15:00:00Z",
            "price": "60000.5",
            "qty": "0.01",
            "execution_id": "exec-1",
            "order": {"id": "brk-1", "client_order_id": "coid-1", "status": "partially_filled"},
        }
    )
    assert event.is_execution and event.filled_qty == 0.01
    assert event.execution_id == "exec-1"
    assert event.status == "partially_filled"  # REST vocabulary differs from the event name
    assert _to_event({"event": "fill"}) is None  # no order object → nothing to apply


# --- venue quirks -----------------------------------------------------------


def test_crypto_notional_is_converted_to_quantity():
    """Alpaca crypto only supports TIF gtc/ioc, and notional requires day → send qty."""
    qty, notional, tif = venue_order_params(
        OrderIntent("BTC/USD", Side.BUY, notional=6_000.0), BTC, price=60_000.0
    )
    assert qty == pytest.approx(0.1) and notional is None and tif == "gtc"

    # Equities keep notional, with the TIF that notional requires.
    qty, notional, tif = venue_order_params(
        OrderIntent("SPY", Side.BUY, notional=500.0), SPY, price=600.0
    )
    assert qty is None and notional == 500.0 and tif == "day"


# --- executor ---------------------------------------------------------------


class FakeAlpaca:
    name = "fake"

    def __init__(self, *, fail: str = "", positions: list[BrokerPosition] | None = None) -> None:
        self.submitted: list[dict] = []
        self.fail = fail
        self._positions = positions or []
        self.orders: dict[str, BrokerOrder] = {}
        self.closed_all = 0

    async def submit_order(self, *, client_order_id, symbol, side, qty=None, notional=None, **kw):
        self.submitted.append({"client_order_id": client_order_id, "symbol": symbol, "qty": qty})
        if self.fail == "duplicate":
            raise DuplicateOrder("client_order_id must be unique")
        if self.fail == "error":
            raise BrokerError("venue rejected")
        order = BrokerOrder(client_order_id, f"brk-{len(self.submitted)}", symbol, side, "new")
        self.orders[client_order_id] = order
        return order

    async def get_order_by_client_id(self, client_order_id):
        return self.orders.get(client_order_id)

    async def get_positions(self):
        return self._positions

    async def close_all_positions(self, cancel_orders: bool = True):
        self.closed_all += 1
        return [BrokerOrder("c", "brk-flat", p.symbol, "sell", "new") for p in self._positions]

    async def is_healthy(self):
        return True

    async def get_account(self):  # pragma: no cover - unused here
        raise NotImplementedError

    async def cancel_order(self, broker_order_id):  # pragma: no cover
        raise NotImplementedError

    async def close_position(self, symbol):  # pragma: no cover
        raise NotImplementedError

    def stream_trade_updates(self):  # pragma: no cover
        raise NotImplementedError


async def approved_outcome(db: Database, notional: float = 500.0):
    guard = RiskGuard(db, LIMITS)
    return await guard.evaluate(
        OrderIntent("BTC/USD", Side.BUY, notional=notional),
        BTC,
        price=60_000.0,
        account=AccountState(equity=100_000.0),
        now=NOW,
        realized_vol=0.02,
    )


async def test_denied_intent_can_never_reach_the_broker(db: Database):
    """THE no-bypass property, end to end: a denied outcome cannot be executed."""
    from trader.risk.breakers import BreakerStore

    await BreakerStore(db).pause("test")
    outcome = await approved_outcome(db)
    assert not outcome.approved

    broker = FakeAlpaca()
    with pytest.raises(RiskDenied):
        await Executor(db, broker).execute(outcome, BTC, price=60_000.0)
    assert broker.submitted == []  # the venue was never contacted
    assert await db.fetch_all("SELECT * FROM orders") == []


async def test_order_row_is_persisted_before_submit(db: Database):
    outcome = await approved_outcome(db)
    broker = FakeAlpaca()
    result = await Executor(db, broker).execute(outcome, BTC, price=60_000.0)

    assert result.submitted
    row = await db.fetch_one("SELECT * FROM orders WHERE id = %s", (result.order_id,))
    assert row["status"] == "submitted"
    assert row["client_order_id"] == result.client_order_id
    assert row["broker_order_id"] == "brk-1"
    assert float(row["qty"]) == pytest.approx(500.0 / 60_000.0)  # crypto → qty
    assert row["time_in_force"] == "gtc"

    audit_rows = await db.fetch_all("SELECT * FROM audit_events WHERE action = 'submitted'")
    assert len(audit_rows) == 1


async def test_duplicate_submission_adopts_the_existing_order(db: Database):
    """A 422 means a previous submit landed — adopt it, never place a second order."""
    outcome = await approved_outcome(db)
    broker = FakeAlpaca(fail="duplicate")
    executor = Executor(db, broker)

    # Seed the "already landed" order under the id the executor will generate.
    original_submit = broker.submit_order

    async def submit_then_register(*, client_order_id, symbol, side, **kw):
        broker.orders[client_order_id] = BrokerOrder(
            client_order_id, "brk-existing", symbol, side, "new"
        )
        return await original_submit(
            client_order_id=client_order_id, symbol=symbol, side=side, **kw
        )

    broker.submit_order = submit_then_register
    result = await executor.execute(outcome, BTC, price=60_000.0)

    assert result.submitted and result.broker_order_id == "brk-existing"
    assert len(broker.submitted) == 1  # exactly one venue call


async def test_broker_error_marks_the_order_and_does_not_raise(db: Database):
    outcome = await approved_outcome(db)
    result = await Executor(db, FakeAlpaca(fail="error")).execute(outcome, BTC, price=60_000.0)

    assert not result.submitted and "venue rejected" in result.reason
    row = await db.fetch_one("SELECT * FROM orders WHERE id = %s", (result.order_id,))
    assert row["status"] == "error"  # journaled, loop continues (opens are best-effort)


async def test_fills_build_the_position_and_dedupe_on_replay(db: Database):
    outcome = await approved_outcome(db)
    broker = FakeAlpaca()
    executor = Executor(db, broker)
    result = await executor.execute(outcome, BTC, price=60_000.0)

    event = OrderEvent(
        event="fill",
        client_order_id=result.client_order_id,
        broker_order_id="brk-1",
        status="filled",
        ts=NOW,
        filled_qty=0.008,
        fill_price=60_000.0,
        execution_id="exec-1",
    )
    assert await executor.apply_event(event) is True
    # The stream reconnects and replays the same execution: it must not double-count.
    assert await executor.apply_event(event) is False

    position = await db.fetch_one("SELECT * FROM positions WHERE symbol = 'BTC/USD'")
    assert float(position["qty"]) == pytest.approx(0.008)
    assert float(position["avg_entry_price"]) == 60_000.0
    fills = await db.fetch_all("SELECT * FROM fills")
    assert len(fills) == 1

    order = await db.fetch_one("SELECT * FROM orders WHERE id = %s", (result.order_id,))
    assert order["status"] == "filled"


async def test_sell_fill_reduces_the_position(db: Database):
    from trader.store.repos import OrdersRepo, PositionsRepo

    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=0.5, avg_entry_price=60_000.0
    )
    order_id = await OrdersRepo(db).insert(
        client_order_id="coid-sell", symbol="BTC/USD", asset_class="crypto", side="sell", qty=0.2
    )
    assert order_id
    await Executor(db, FakeAlpaca()).apply_event(
        OrderEvent(
            event="fill",
            client_order_id="coid-sell",
            broker_order_id="brk-2",
            status="filled",
            ts=NOW,
            filled_qty=0.2,
            fill_price=61_000.0,
            execution_id="exec-2",
        )
    )
    position = await db.fetch_one("SELECT * FROM positions WHERE symbol = 'BTC/USD'")
    assert float(position["qty"]) == pytest.approx(0.3)
    assert float(position["avg_entry_price"]) == 60_000.0  # a reduction keeps the basis


async def test_unknown_order_event_is_ignored(db: Database):
    applied = await Executor(db, FakeAlpaca()).apply_event(
        OrderEvent("fill", "not-ours", "brk-9", "filled", NOW, 1.0, 100.0, "exec-9")
    )
    assert applied is False


# --- restart / reconcile kill-tests ----------------------------------------


async def test_restart_recovers_orders_left_in_flight(db: Database):
    """Kill-test: a crash between persist and ack must resolve via the idempotency key."""
    from trader.store.repos import OrdersRepo

    landed = await OrdersRepo(db).insert(
        client_order_id="coid-landed", symbol="BTC/USD", asset_class="crypto", side="buy", qty=0.01
    )
    never = await OrdersRepo(db).insert(
        client_order_id="coid-never", symbol="BTC/USD", asset_class="crypto", side="buy", qty=0.01
    )
    broker = FakeAlpaca()
    broker.orders["coid-landed"] = BrokerOrder("coid-landed", "brk-7", "BTC/USD", "buy", "filled")

    recovered = await Executor(db, broker).recover_pending()
    assert {r["order_id"]: r["resolved"] for r in recovered} == {
        landed: "filled",
        never: "never_landed",
    }
    rows = {r["id"]: r["status"] for r in await db.fetch_all("SELECT * FROM orders")}
    assert rows[landed] == "filled" and rows[never] == "error"


async def test_position_drift_pauses_routing_instead_of_crashing(db: Database):
    from trader.risk.breakers import BreakerStore
    from trader.store.repos import PositionsRepo

    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.5)
    broker = FakeAlpaca(positions=[BrokerPosition("BTC/USD", qty=0.2, qty_available=0.2)])

    result = await Executor(db, broker).reconcile_positions()
    assert result["drift"]["BTC/USD"] == {"local": 0.5, "venue": 0.2}
    state = await BreakerStore(db).load()
    assert state.drift_detected and "drift" in state.blocked_reason


async def test_clean_reconcile_leaves_routing_alone(db: Database):
    from trader.risk.breakers import BreakerStore
    from trader.store.repos import PositionsRepo

    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.25)
    broker = FakeAlpaca(positions=[BrokerPosition("BTC/USD", qty=0.25, qty_available=0.25)])
    assert (await Executor(db, broker).reconcile_positions())["drift"] == {}
    assert not (await BreakerStore(db).load()).drift_detected


async def test_adopting_venue_positions_clears_drift(db: Database):
    from trader.risk.breakers import BreakerStore
    from trader.store.repos import PositionsRepo

    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.5)
    broker = FakeAlpaca(
        positions=[BrokerPosition("BTC/USD", qty=0.2, qty_available=0.2, asset_class="crypto")]
    )
    executor = Executor(db, broker)
    await executor.reconcile_positions()
    assert (await BreakerStore(db).load()).drift_detected

    assert await executor.adopt_venue_positions() == 1
    position = await db.fetch_one("SELECT * FROM positions WHERE symbol = 'BTC/USD'")
    assert float(position["qty"]) == 0.2  # the venue is the source of truth
    assert not (await BreakerStore(db).load()).drift_detected


# --- flatten ----------------------------------------------------------------


async def test_flatten_all_is_confirm_gated(db: Database):
    broker = FakeAlpaca(positions=[BrokerPosition("BTC/USD", 0.5, 0.5)])
    executor = Executor(db, broker)

    for wrong in ("", "flatten", "yes", "FLATTEN "):
        assert (await executor.flatten_all(confirm=wrong))["flattened"] is False
    assert broker.closed_all == 0  # a typo must never liquidate the book

    assert (await executor.flatten_all(confirm="FLATTEN"))["flattened"] is True
    assert broker.closed_all == 1
    assert await db.fetch_all("SELECT * FROM audit_events WHERE action = 'flatten_all'")


# --- loop integration -------------------------------------------------------


async def test_live_paper_loop_executes_and_journals(db: Database):
    """Shadow off + executor attached: the decision reaches the venue and is journaled."""
    from tests.test_loop import OPEN, StubLLM, StubMarketData
    from trader.llm.registry import LLMRegistry
    from trader.loop.journal import EXECUTED, Journal
    from trader.loop.runner import LoopRunner

    broker = FakeAlpaca()
    runner = LoopRunner(
        db=db,
        market_data=StubMarketData(),
        llm=StubLLM([OPEN]),
        guard=RiskGuard(db, LIMITS),
        symbols=[BTC],
        shadow=False,
        equity=100_000.0,
        executor=Executor(db, broker),
    )
    assert isinstance(runner.llm, StubLLM | LLMRegistry)
    results = await runner.tick(NOW)

    assert [r.status for r in results] == [EXECUTED]
    assert len(broker.submitted) == 1
    row = await Journal(db).get(results[0].decision_id)
    assert row["status"] == EXECUTED
    order = await db.fetch_one("SELECT * FROM orders")
    assert order["status"] == "submitted" and order["symbol"] == "BTC/USD"


async def test_shadow_loop_never_reaches_the_venue_even_with_an_executor(db: Database):
    """Belt and braces: shadow=True submits nothing even when a broker is attached."""
    from tests.test_loop import OPEN, StubLLM, StubMarketData
    from trader.loop.runner import LoopRunner

    broker = FakeAlpaca()
    runner = LoopRunner(
        db=db,
        market_data=StubMarketData(),
        llm=StubLLM([OPEN]),
        guard=RiskGuard(db, LIMITS),
        symbols=[BTC],
        shadow=True,
        equity=100_000.0,
        executor=Executor(db, broker),
    )
    await runner.tick(NOW)
    assert broker.submitted == []
    assert await db.fetch_all("SELECT * FROM orders") == []


# --- the stream must actually run, and must not die (EAI-176 acceptance finding) ------


async def test_run_stream_survives_disconnects_and_applies_later_events(db: Database):
    """2026-09-11: the FIRST live fill never reached the ledger — run_stream existed but
    was (a) never started and (b) fatal on its first error despite its own docstring.
    The stream must reconnect with backoff; venue disconnects are routine."""
    import asyncio

    outcome = await approved_outcome(db)
    broker = FakeAlpaca()
    executor = Executor(db, broker)
    result = await executor.execute(outcome, BTC, price=60_000.0)

    applied = asyncio.Event()
    calls = {"n": 0}

    async def flaky_stream():
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerError("ws dropped")  # first connect dies mid-stream
        yield OrderEvent(
            event="fill",
            client_order_id=result.client_order_id,
            broker_order_id="brk-1",
            status="filled",
            ts=NOW,
            filled_qty=0.008,
            fill_price=60_000.0,
            execution_id="exec-stream-1",
        )
        applied.set()
        await asyncio.sleep(3600)  # hold the stream open like a real socket

    broker.stream_trade_updates = flaky_stream
    task = asyncio.create_task(executor.run_stream(reconnect_delay=0.05))
    try:
        await asyncio.wait_for(applied.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls["n"] == 2  # died once, reconnected, served
    fills = await db.fetch_all("SELECT * FROM fills")
    assert len(fills) == 1  # the fill landed in the ledger


async def test_runner_starts_and_stops_the_executor_stream(db: Database):
    """LoopRunner owns the executor: starting the loop must start the fills stream,
    stopping it must cancel it. A live-paper build without the stream is a ledger
    that silently diverges on the first fill."""
    import asyncio

    from tests.test_loop import build_runner

    class StreamProbe:
        def __init__(self) -> None:
            self.running = asyncio.Event()
            self.cancelled = False

        async def run_stream(self, reconnect_delay: float = 5.0) -> None:
            self.running.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    probe = StreamProbe()
    runner = build_runner(db, [], executor=probe)
    await runner.start()
    try:
        await asyncio.wait_for(probe.running.wait(), timeout=2)
    finally:
        await runner.stop()
    assert probe.cancelled
