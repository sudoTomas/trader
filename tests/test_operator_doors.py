"""EAI-195: the operator doors the first live fill proved missing — approvals
resolution and drift adoption, registry-first across the three surfaces. Both were
performed via direct service calls on 2026-09-11; that must never be necessary again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from trader.brokers.base import BrokerOrder
from trader.core.types import OrderIntent, Quote, Side
from trader.data.base import MarketDataError
from trader.loop.executor import Executor
from trader.risk.approvals import ApprovalService
from trader.risk.breakers import BreakerStore
from trader.risk.guard import RiskGuard
from trader.risk.limits import RiskLimits
from trader.store.db import Database
from trader.surfaces.services import build_registry


def test_operator_door_capabilities_are_registered():
    tools = {c.tool_name for c in build_registry(None).all()}
    assert {"resolve_approval", "adopt_venue_positions"} <= tools


async def park_one(db: Database) -> int:
    return await ApprovalService(db).park(
        OrderIntent("SPY", Side.BUY, notional=5000.0),
        price=760.0,
        reason="approval_threshold: over",
    )


# --- approvals resolution ----------------------------------------------------


async def test_deny_resolves_the_row_and_audits(db: Database):
    approval_id = await park_one(db)
    registry = build_registry(db)
    out = await registry.get("resolve_approval").handler(
        approval_id=approval_id, approved=False, resolved_by="tester"
    )
    assert out["resolved"] is True and out["status"] == "denied"

    row = await db.fetch_one("SELECT * FROM approvals WHERE id = %s", (approval_id,))
    assert row["status"] == "denied" and row["resolved_by"] == "tester"
    audit = await db.fetch_one(
        "SELECT payload FROM audit_events WHERE action = 'resolve_approval'"
        " ORDER BY id DESC LIMIT 1"
    )
    assert audit["payload"]["resolved_by"] == "tester"

    # Resolving a non-pending row is a no-op, not an error.
    again = await registry.get("resolve_approval").handler(
        approval_id=approval_id, approved=True, resolved_by="tester"
    )
    assert again["resolved"] is False


async def park_btc(db: Database, *, notional: float = 5_000.0, price: float = 60_000.0) -> int:
    return await ApprovalService(db).park(
        OrderIntent("BTC/USD", Side.BUY, notional=notional),
        price=price,
        reason="park band",
    )


# Caps sit under the parked size so a guard re-run that forgot the human already
# approved would clamp to 1_000 or park again instead of submitting 5_000.
REDEEM_LIMITS = RiskLimits(
    max_position_frac=0.50,
    approval_notional_usd=1_000.0,
    park_above_usd=4_000.0,
)


class RecordingBroker:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit_order(self, *, client_order_id, symbol, side, qty=None, notional=None, **kw):
        self.submitted.append(
            {
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "notional": notional,
            }
        )
        return BrokerOrder(client_order_id, "brk-1", symbol, side, "new")


class FixedQuote:
    def __init__(self, mid: float) -> None:
        self.mid = mid

    async def get_latest_quote(self, symbol):
        now = datetime.now(tz=UTC)
        return Quote(symbol.ticker, now, self.mid, self.mid)


class DeadFeed:
    async def get_latest_quote(self, symbol):
        raise MarketDataError("quote unavailable")


class RedeemRunner:
    def __init__(self, db: Database, broker: Any, *, mid: float, limits: RiskLimits, equity: float):
        self.market_data = FixedQuote(mid)
        self.guard = RiskGuard(db, limits)
        self.equity = equity
        self.executor = Executor(db, broker)
        self.shadow = False


async def approve(db: Database, approval_id: int, runner: Any) -> dict[str, Any]:
    return (
        await build_registry(db, runner=runner)
        .get("resolve_approval")
        .handler(approval_id=approval_id, approved=True, resolved_by="tester")
    )


async def test_approve_without_executor_does_not_trade(db: Database):
    """Shadow builds have no executor. Approve must say so and must not imply a fill."""
    approval_id = await park_btc(db)
    out = (
        await build_registry(db)
        .get("resolve_approval")
        .handler(approval_id=approval_id, approved=True, resolved_by="tester")
    )
    assert out["resolved"] is True and out["status"] == "approved"
    assert out["redeemed"] is False
    assert out["reason"]
    assert "note" not in out


async def test_approve_redeems_through_the_guard_and_submits(db: Database):
    """The parked size trades. The unattended cap must not clamp a human approval back down."""
    approval_id = await park_btc(db, notional=5_000.0, price=60_000.0)
    broker = RecordingBroker()
    runner = RedeemRunner(db, broker, mid=60_000.0, limits=REDEEM_LIMITS, equity=100_000.0)
    out = await approve(db, approval_id, runner)
    assert out["resolved"] is True and out["status"] == "approved"
    assert out["redeemed"] is True
    assert "note" not in out
    assert len(broker.submitted) == 1
    order = broker.submitted[0]
    assert order["symbol"] == "BTCUSD"
    assert order["notional"] is None
    assert order["qty"] == pytest.approx(5_000.0 / 60_000.0)
    row = await db.fetch_one("SELECT qty FROM orders")
    assert row is not None
    assert float(row["qty"]) == pytest.approx(5_000.0 / 60_000.0)

    again = await approve(db, approval_id, runner)
    assert again["resolved"] is False
    assert len(broker.submitted) == 1  # a second approve must not double-submit


async def test_approve_sizes_the_submit_off_the_fresh_price(db: Database):
    approval_id = await park_btc(db, price=60_000.0)
    mid = 60_300.0  # +0.5%, inside the default 1% tolerance
    broker = RecordingBroker()
    runner = RedeemRunner(db, broker, mid=mid, limits=REDEEM_LIMITS, equity=100_000.0)
    out = await approve(db, approval_id, runner)
    assert out["redeemed"] is True
    assert broker.submitted[0]["qty"] == pytest.approx(5_000.0 / mid)


async def test_approve_refuses_a_stale_price_and_does_not_submit(db: Database):
    approval_id = await park_btc(db, price=60_000.0)
    broker = RecordingBroker()
    limits = RiskLimits(
        max_position_frac=0.50,
        approval_notional_usd=1_000.0,
        park_above_usd=4_000.0,
        approval_price_tolerance_frac=0.001,
    )
    runner = RedeemRunner(db, broker, mid=60_300.0, limits=limits, equity=100_000.0)
    out = await approve(db, approval_id, runner)
    assert out["redeemed"] is False
    assert "price" in out["reason"]
    assert broker.submitted == []


async def test_approve_refuses_an_expired_approval(db: Database):
    approval_id = await park_btc(db)
    await db.execute(
        "UPDATE approvals SET created_at = now() - interval '2 hours' WHERE id = %s",
        (approval_id,),
    )
    broker = RecordingBroker()
    runner = RedeemRunner(db, broker, mid=60_000.0, limits=REDEEM_LIMITS, equity=100_000.0)
    out = await approve(db, approval_id, runner)
    assert out["redeemed"] is False
    assert "expired" in out["reason"]
    assert broker.submitted == []


async def test_approve_reruns_the_guard_and_does_not_bypass_it(db: Database):
    approval_id = await park_btc(db)
    await BreakerStore(db).pause("maintenance")
    broker = RecordingBroker()
    runner = RedeemRunner(db, broker, mid=60_000.0, limits=REDEEM_LIMITS, equity=100_000.0)
    out = await approve(db, approval_id, runner)
    assert out["redeemed"] is False
    assert "kill_switch" in out["reason"]
    assert broker.submitted == []


async def test_redeemed_open_still_obeys_the_position_cap(db: Database):
    """Human approval clears the park band, not the position cap."""
    approval_id = await park_btc(db, notional=50_000.0)
    broker = RecordingBroker()
    limits = RiskLimits(
        max_position_frac=0.05,
        approval_notional_usd=1_000.0,
        park_above_usd=4_000.0,
    )
    runner = RedeemRunner(db, broker, mid=60_000.0, limits=limits, equity=100_000.0)
    out = await approve(db, approval_id, runner)
    assert out["redeemed"] is True
    assert broker.submitted[0]["qty"] == pytest.approx(5_000.0 / 60_000.0)


async def test_redeemed_dust_is_not_submitted(db: Database):
    approval_id = await park_btc(db, notional=100.0)
    broker = RecordingBroker()
    limits = RiskLimits(max_position_frac=0.05, min_notional_usd=5.0, approval_notional_usd=1_000.0)
    runner = RedeemRunner(db, broker, mid=60_000.0, limits=limits, equity=40.0)
    out = await approve(db, approval_id, runner)
    assert out["redeemed"] is False
    assert "min_notional" in out["reason"]
    assert broker.submitted == []


async def test_approve_does_not_submit_without_a_fresh_price(db: Database):
    approval_id = await park_btc(db)
    broker = RecordingBroker()
    runner = RedeemRunner(db, broker, mid=60_000.0, limits=REDEEM_LIMITS, equity=100_000.0)
    runner.market_data = DeadFeed()
    out = await approve(db, approval_id, runner)
    assert out["redeemed"] is False
    assert broker.submitted == []


async def test_approve_unknown_symbol_does_not_submit(db: Database):
    approval_id = await ApprovalService(db).park(
        OrderIntent("NOPE", Side.BUY, notional=5_000.0), price=10.0, reason="unknown"
    )
    broker = RecordingBroker()
    runner = RedeemRunner(db, broker, mid=10.0, limits=REDEEM_LIMITS, equity=100_000.0)
    out = await approve(db, approval_id, runner)
    assert out["redeemed"] is False
    assert broker.submitted == []


# --- drift adoption ----------------------------------------------------------


class AdoptProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def adopt_venue_positions(self) -> int:
        self.calls += 1
        return 2


class RunnerStub:
    def __init__(self, executor: Any | None) -> None:
        self.executor = executor


async def test_adopt_is_confirm_gated_and_audited(db: Database):
    probe = AdoptProbe()
    registry = build_registry(db, runner=RunnerStub(probe))

    refused = await registry.get("adopt_venue_positions").handler(confirm="")
    assert refused["adopted"] is False and probe.calls == 0

    out = await registry.get("adopt_venue_positions").handler(confirm="ADOPT")
    assert out["adopted"] is True and out["positions"] == 2 and probe.calls == 1
    audit = await db.fetch_one(
        "SELECT id FROM audit_events WHERE action = 'adopt_venue_positions'"
        " AND actor = 'surface' ORDER BY id DESC LIMIT 1"
    )
    assert audit is not None


async def test_adopt_without_a_broker_reports_why(db: Database):
    registry = build_registry(db, runner=RunnerStub(None))
    out = await registry.get("adopt_venue_positions").handler(confirm="ADOPT")
    assert out["adopted"] is False and "broker" in out["reason"]


# --- the API + CLI doors -----------------------------------------------------


def test_doors_exist_on_api_and_cli(clean_db: None, monkeypatch):
    import asyncio
    import json

    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app

    async def seed() -> int:
        database = Database(min_size=1, max_size=1)
        await database.open()
        try:
            return await park_one(database)
        finally:
            await database.close()

    approval_id = asyncio.run(seed())
    dev = Settings(auth_disabled=True)

    with TestClient(create_app(dev)) as tc:
        out = tc.post(
            f"/risk/approvals/{approval_id}/resolve",
            params={"approved": False, "resolved_by": "api-test"},
        ).json()
        assert out["resolved"] is True and out["status"] == "denied"
        adopt = tc.post("/broker/adopt", params={"confirm": "ADOPT"}).json()
        assert adopt["adopted"] is False  # no broker in this build — honest reason
        assert "broker" in adopt["reason"]

    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(dev)))
    result = CliRunner().invoke(cli.app, ["risk", "deny", str(approval_id)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["resolved"] is False  # already resolved above

    result = CliRunner().invoke(cli.app, ["broker", "adopt", "--confirm", "ADOPT"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["adopted"] is False  # no broker in this build
