"""The deterministic stop (#74): a held position past its stop-loss is closed by the loop,
not the model. Seams: ``LoopRunner.tick`` (the loop decides and journals the stop),
``RiskGuard.evaluate`` (the re-entry cooldown), and risk status on every surface.

The venue is a FakeAlpaca, the store is real, and the decide model is scripted so a
call it should never get fails the test.
"""

from __future__ import annotations

from datetime import timedelta

from tests.test_executor import FakeAlpaca
from tests.test_loop import BTC, HOLD, NOW, StubLLM, StubMarketData
from trader.loop.executor import Executor
from trader.loop.journal import DENIED, EXECUTED, SUPPRESSED
from trader.loop.runner import LoopRunner
from trader.risk.guard import RiskGuard
from trader.risk.limits import RiskLimits
from trader.store.db import Database
from trader.store.repos import PositionsRepo

# StubMarketData quotes 159.0, so an entry of 200 is a 20.5% loss.
PRICE = 159.0
LIMITS = RiskLimits(max_position_frac=0.05, approval_notional_usd=100_000.0, stop_loss_frac=0.08)


async def hold_btc(db: Database, *, qty: float = 0.5, entry: float | None = 200.0) -> None:
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=qty, avg_entry_price=entry
    )


def runner_for(db: Database, broker: FakeAlpaca, llm: StubLLM, **kw) -> LoopRunner:
    return LoopRunner(
        db=db,
        market_data=StubMarketData(),
        llm=llm,
        guard=RiskGuard(db, kw.pop("limits", LIMITS)),
        symbols=[BTC],
        shadow=False,
        equity=100_000.0,
        executor=Executor(db, broker),
        **kw,
    )


async def test_a_loss_past_the_stop_on_two_ticks_closes_the_whole_position(db: Database):
    await hold_btc(db)
    broker = FakeAlpaca()
    llm = StubLLM([HOLD])  # one decide on the first tick; a second call would fail
    runner = runner_for(db, broker, llm)

    first = await runner.tick(NOW)
    assert broker.submitted == []  # one crossing is not enough: it could be a bad quote
    assert [r.triggers for r in first] != [["stop_loss"]]

    second = await runner.tick(NOW + timedelta(seconds=60))

    assert len(llm.calls) == 1  # the stop pre-empted the model
    assert [(r.symbol, r.triggers, r.status, r.action) for r in second] == [
        ("BTC/USD", ["stop_loss"], EXECUTED, "close")
    ]
    assert broker.submitted[0]["qty"] == 0.5  # the whole position, by qty
    decision = await db.fetch_one("SELECT * FROM decisions WHERE id = %s", (second[0].decision_id,))
    assert decision["trigger"] == "stop_loss" and decision["status"] == EXECUTED
    assert decision["prompt"] is None and decision["cost_usd"] in (None, 0)
    assert "-20.5%" in decision["decision"]["rationale"]
    order = await db.fetch_one("SELECT * FROM orders")
    assert order["decision_id"] == decision["id"] and order["side"] == "sell"


async def ticks(runner: LoopRunner, n: int, start: int = 0):
    out = []
    for i in range(start, start + n):
        out.append(await runner.tick(NOW + timedelta(seconds=60 * i)))
    return out


async def stop_rows(db: Database) -> list[dict]:
    return await db.fetch_all(
        "SELECT status, raw_output FROM decisions WHERE trigger = 'stop_loss' ORDER BY id"
    )


async def test_pause_blocks_the_stop_which_journals_once_and_fires_on_resume(db: Database):
    await hold_btc(db)
    broker = FakeAlpaca()
    runner = runner_for(db, broker, StubLLM([HOLD]))
    await runner.guard.breakers.pause("operator")

    await ticks(runner, 5)  # crossing, then four blocked retries

    assert broker.submitted == []
    assert [r["status"] for r in await stop_rows(db)] == [DENIED]  # once, not per tick
    denied = await db.fetch_one("SELECT risk_verdicts FROM decisions WHERE trigger = 'stop_loss'")
    assert denied["risk_verdicts"][-1]["check"] == "kill_switch"

    await runner.guard.breakers.unpause()
    await ticks(runner, 1, start=5)

    assert len(broker.submitted) == 1
    assert [r["status"] for r in await stop_rows(db)] == [DENIED, EXECUTED]


async def test_an_open_stop_order_is_not_resent(db: Database):
    await hold_btc(db)
    broker = FakeAlpaca()
    runner = runner_for(db, broker, StubLLM([HOLD]))

    await ticks(runner, 6)  # the fill never arrives, so the position stays on the ledger

    assert len(broker.submitted) == 1
    assert [r["status"] for r in await stop_rows(db)] == [EXECUTED]


async def test_a_position_without_an_entry_is_journaled_unprotected_once(db: Database):
    await hold_btc(db, entry=None)
    runner = runner_for(db, FakeAlpaca(), StubLLM([HOLD]))

    await ticks(runner, 3)

    assert [(r["status"], r["raw_output"]) for r in await stop_rows(db)] == [
        (SUPPRESSED, "no stop: entry unknown")
    ]


async def test_a_recovered_price_resets_the_two_tick_count(db: Database):
    await hold_btc(db)
    broker = FakeAlpaca()
    data = StubMarketData()
    runner = runner_for(db, broker, StubLLM([HOLD, HOLD, HOLD]))
    runner.market_data = data

    await runner.tick(NOW)  # crossing 1
    data._series = [*data._series[:-1], 199.0]  # back inside the stop
    await runner.tick(NOW + timedelta(seconds=60))
    data._series = [*data._series[:-1], PRICE]
    await runner.tick(NOW + timedelta(seconds=120))  # crossing 1 again, not 2

    assert broker.submitted == []


async def test_the_stop_is_off_at_zero(db: Database):
    await hold_btc(db)
    broker = FakeAlpaca()
    off = RiskLimits(max_position_frac=0.05, approval_notional_usd=100_000.0)
    runner = runner_for(db, broker, StubLLM([HOLD]), limits=off)

    await ticks(runner, 3)

    assert broker.submitted == [] and await stop_rows(db) == []


async def stopped_out(db: Database, at) -> None:
    """A stop that went out for BTC at ``at`` (the journal stamps wall-clock time)."""
    from trader.loop.journal import Journal

    decision_id = await Journal(db).record(trigger="stop_loss", status=EXECUTED, symbol="BTC/USD")
    await db.execute("UPDATE decisions SET ts = %s WHERE id = %s", (at, decision_id))


async def guard_says(db: Database, side, at, *, held: float = 0.0):
    from trader.core.types import OrderIntent
    from trader.risk.guard import AccountState

    return await RiskGuard(db, LIMITS).evaluate(
        OrderIntent("BTC/USD", side, notional=500.0),
        BTC,
        price=PRICE,
        account=AccountState(equity=100_000.0, positions={"BTC/USD": held}),
        now=at,
        realized_vol=0.02,
    )


async def test_a_stopped_symbol_takes_no_opens_until_the_cooldown_ends(db: Database):
    from trader.core.types import Side

    await stopped_out(db, NOW)

    inside = await guard_says(db, Side.BUY, NOW + timedelta(hours=23))
    assert not inside.approved
    assert inside.verdicts[-1].check == "stop_cooldown"
    assert "stopped out" in inside.reason

    after = await guard_says(db, Side.BUY, NOW + timedelta(hours=25))
    assert after.approved

    # Exits are never trapped by it: a close inside the window still goes.
    close = await guard_says(db, Side.SELL, NOW + timedelta(hours=1), held=0.5)
    assert close.approved


async def test_a_blocked_or_shadow_stop_starts_no_cooldown(db: Database):
    from trader.core.types import Side
    from trader.loop.journal import Journal

    for status in (DENIED, "shadow"):
        decision_id = await Journal(db).record(trigger="stop_loss", status=status, symbol="BTC/USD")
        await db.execute("UPDATE decisions SET ts = %s WHERE id = %s", (NOW, decision_id))

    assert (await guard_says(db, Side.BUY, NOW + timedelta(hours=1))).approved


def test_risk_status_shows_each_position_s_stop_on_every_door(clean_db: None, monkeypatch):
    """Registry → API → MCP → CLI: the stop, each held position's stop price, and the
    positions it cannot protect."""
    import asyncio
    import json

    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    async def seed_then_mcp() -> dict:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            await hold_btc(db, qty=0.5, entry=200.0)
            await PositionsRepo(db).upsert(
                symbol="ETH/USD", asset_class="crypto", qty=2.0, avg_entry_price=None
            )
            async with Client(build_mcp(build_registry(db))) as mcp_client:
                result = await mcp_client.call_tool("get_risk_status", {})
            assert not result.is_error, result.content
            return result.structured_content
        finally:
            await db.close()

    via_mcp = asyncio.run(seed_then_mcp())
    settings = Settings(auth_disabled=True)
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    with TestClient(create_app(settings)) as tc:
        via_api = tc.get("/risk/status").json()
    printed = CliRunner().invoke(cli.app, ["risk", "status"])
    assert printed.exit_code == 0, printed.output

    assert via_api["stops"] == {
        "stop_loss_frac": 0.08,
        "cooldown_seconds": 86_400,
        "positions": [
            {"symbol": "BTC/USD", "qty": 0.5, "avg_entry_price": 200.0, "stop_price": 184.0},
            {"symbol": "ETH/USD", "qty": 2.0, "avg_entry_price": None, "stop_price": None},
        ],
        "unprotected": ["ETH/USD"],
    }
    assert json.loads(printed.output)["stops"] == via_api["stops"] == via_mcp["stops"]
