"""EAI-195: the operator doors the first live fill proved missing — approvals
resolution and drift adoption, registry-first across the three surfaces. Both were
performed via direct service calls on 2026-09-11; that must never be necessary again.
"""

from __future__ import annotations

from typing import Any

from trader.core.types import OrderIntent, Side
from trader.risk.approvals import ApprovalService
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


async def test_approve_is_honest_about_unwired_execution(db: Database):
    """Approve flips the status — but nothing redeems approved rows yet (redeem() has
    no callers). The door must say so rather than imply a trade will happen."""
    approval_id = await park_one(db)
    registry = build_registry(db)
    out = await registry.get("resolve_approval").handler(
        approval_id=approval_id, approved=True, resolved_by="tester"
    )
    assert out["resolved"] is True and out["status"] == "approved"
    assert "not" in out["note"].lower() and "execut" in out["note"].lower()


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
