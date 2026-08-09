"""Store layer tests — real Postgres (compose db on :5544), no mocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg.errors
import pytest

from trader.store.audit import audit
from trader.store.db import Database
from trader.store.migrate import MigrationError, run_migrations
from trader.store.repos import (
    ApprovalsRepo,
    BarsRepo,
    DecisionsRepo,
    MacroViewsRepo,
    OrdersRepo,
    PositionsRepo,
    ResearchNotesRepo,
    UsageRepo,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


# --- migrations -------------------------------------------------------------


async def test_migrations_are_idempotent(db: Database):
    # The session fixture already applied the baseline; a second run applies nothing.
    assert await run_migrations(db) == []


async def test_migration_runner_fails_hard_and_stops(db: Database, tmp_path: Path):
    (tmp_path / "9001_ok.sql").write_text("CREATE TABLE _mig_test_ok (id int);")
    (tmp_path / "9002_bad.sql").write_text("CREATE TABLE _mig_test_bad (id int); THIS IS NOT SQL;")
    (tmp_path / "9003_never.sql").write_text("CREATE TABLE _mig_test_never (id int);")

    try:
        with pytest.raises(MigrationError, match="9002_bad.sql"):
            await run_migrations(db, migrations_dir=tmp_path)

        applied = {
            r["name"]
            for r in await db.fetch_all("SELECT name FROM schema_migrations WHERE name LIKE '9%'")
        }
        assert applied == {"9001_ok.sql"}  # the bad file aborted the run; nothing after it ran

        # The failed file's partial DDL rolled back with it.
        for tbl in ("_mig_test_bad", "_mig_test_never"):
            assert await db.fetch_one("SELECT to_regclass(%s) AS t", (tbl,)) == {"t": None}
    finally:
        await db.execute("DROP TABLE IF EXISTS _mig_test_ok, _mig_test_bad, _mig_test_never")
        await db.execute("DELETE FROM schema_migrations WHERE name LIKE '9%'")


# --- audit ------------------------------------------------------------------


async def test_audit_round_trip(db: Database):
    audit_id = await audit(
        db,
        actor="cli",
        category="risk",
        action="pause_trading",
        subject="BTC/USD",
        payload={"reason": "operator"},
    )
    row = await db.fetch_one("SELECT * FROM audit_events WHERE id = %s", (audit_id,))
    assert row is not None
    assert (row["actor"], row["category"], row["action"]) == ("cli", "risk", "pause_trading")
    assert row["payload"] == {"reason": "operator"}
    assert row["ts"] is not None


async def test_audit_events_refuse_update_and_delete(db: Database):
    audit_id = await audit(db, actor="test", category="config", action="x")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await db.execute("UPDATE audit_events SET action = 'y' WHERE id = %s", (audit_id,))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await db.execute("DELETE FROM audit_events WHERE id = %s", (audit_id,))


# --- db.fetch_iter ----------------------------------------------------------


async def test_fetch_iter_streams_in_batches(db: Database):
    bars = BarsRepo(db)
    candles = [
        {
            "symbol": "BTC/USD",
            "interval": "1m",
            "ts": NOW + timedelta(minutes=i),
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100.5 + i,
            "volume": 10,
        }
        for i in range(25)
    ]
    assert await bars.upsert_many(candles) == 25

    seen = [
        row["ts"]
        async for row in db.fetch_iter(
            "SELECT ts FROM bars WHERE symbol = %s ORDER BY ts", ("BTC/USD",), batch_size=10
        )
    ]
    assert len(seen) == 25
    assert seen == sorted(seen)


# --- repos ------------------------------------------------------------------


async def test_decisions_journal_round_trip(db: Database):
    repo = DecisionsRepo(db)
    decision_id = await repo.insert(
        trigger="heartbeat",
        status="shadow",
        symbol="BTC/USD",
        asset_class="crypto",
        prompt="ctx...",
        raw_output='{"action":"hold"}',
        decision={"action": "hold"},
        risk_verdicts=[{"check": "paper_gate", "verdict": "allow"}],
        macro_view_version=None,
        cost_usd=0.0012,
        latency_ms=850,
    )
    row = await repo.get(decision_id)
    assert row is not None
    assert row["decision"] == {"action": "hold"}
    assert row["risk_verdicts"][0]["check"] == "paper_gate"
    assert [r["id"] for r in await repo.recent()] == [decision_id]


async def test_orders_idempotency_and_fill_dedupe(db: Database):
    orders = OrdersRepo(db)
    order_id = await orders.insert(
        client_order_id="coid-1", symbol="BTC/USD", asset_class="crypto", side="buy", qty=0.01
    )
    # client_order_id is the idempotency key — a resubmit must be impossible.
    with pytest.raises(psycopg.errors.UniqueViolation):
        await orders.insert(
            client_order_id="coid-1", symbol="BTC/USD", asset_class="crypto", side="buy", qty=0.01
        )

    await orders.set_status(order_id, "submitted", broker_order_id="brk-1")
    row = await orders.get_by_client_order_id("coid-1")
    assert row is not None and row["status"] == "submitted" and row["broker_order_id"] == "brk-1"

    # Trade-updates streams replay; the same broker fill id must land exactly once.
    fill_id = await orders.add_fill(
        order_id, ts=NOW, qty=0.01, price=60000, broker_fill_id="fill-1"
    )
    assert fill_id > 0
    assert (
        await orders.add_fill(order_id, ts=NOW, qty=0.01, price=60000, broker_fill_id="fill-1") == 0
    )
    assert len(await orders.fills_for(order_id)) == 1


async def test_positions_upsert(db: Database):
    repo = PositionsRepo(db)
    await repo.upsert(symbol="BTC/USD", asset_class="crypto", qty=0.5, avg_entry_price=60000)
    await repo.upsert(symbol="BTC/USD", asset_class="crypto", qty=0.7, avg_entry_price=61000)
    row = await repo.get("BTC/USD")
    assert row is not None and float(row["qty"]) == 0.7
    assert len(await repo.all()) == 1


async def test_research_notes_lifecycle_and_staleness(db: Database):
    notes = ResearchNotesRepo(db)
    live = await notes.add(title="live", body="b", expires_at=NOW + timedelta(days=365))
    stale = await notes.add(title="stale", body="b", expires_at=NOW - timedelta(days=1))
    pinned_stale = await notes.add(
        title="pinned", body="b", pinned=True, expires_at=NOW - timedelta(days=1)
    )
    retired = await notes.add(title="retired", body="b")
    await notes.set_status(retired, "retired")

    current = {r["id"] for r in await notes.list_current()}
    assert live in current and pinned_stale in current  # pinned exempts from auto-stale
    assert stale not in current and retired not in current


async def test_macro_views_are_versioned(db: Database):
    views = MacroViewsRepo(db)
    v1 = await views.add(
        compiled_by="grok", global_thesis="t1", sections={"crypto": "..."}, note_ids=[1]
    )
    v2 = await views.add(
        compiled_by="grok", global_thesis="t2", sections={"crypto": "..."}, note_ids=[1, 2]
    )
    assert v2 > v1
    current = await views.current()
    assert current is not None and current["version"] == v2
    old = await views.get(v1)
    assert old is not None and old["global_thesis"] == "t1"  # replay: old versions stay readable


async def test_bars_cache_is_immutable(db: Database):
    bars = BarsRepo(db)
    candle = {
        "symbol": "ETH/USD",
        "interval": "1h",
        "ts": NOW,
        "open": 1,
        "high": 2,
        "low": 0.5,
        "close": 1.5,
        "volume": 100,
    }
    assert await bars.upsert_many([candle]) == 1
    assert await bars.upsert_many([{**candle, "close": 999}]) == 0  # never overwrite
    rows = await bars.range("ETH/USD", "1h", NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert len(rows) == 1 and float(rows[0]["close"]) == 1.5


async def test_approvals_resolve_exactly_once(db: Database):
    approvals = ApprovalsRepo(db)
    approval_id = await approvals.create(
        order_intent={"symbol": "BTC/USD", "side": "buy", "notional": 5000},
        reason="approval_threshold",
    )
    assert [r["id"] for r in await approvals.pending()] == [approval_id]
    assert await approvals.resolve(approval_id, status="approved", resolved_by="tomas") is True
    assert await approvals.resolve(approval_id, status="denied", resolved_by="tomas") is False
    assert await approvals.pending() == []


async def test_usage_daily_accumulates(db: Database):
    usage = UsageRepo(db)
    for _ in range(2):
        await usage.record(
            day=NOW.date(),
            role="decide",
            provider="nemotron",
            model="nemotron-nano",
            prompt_tokens=1000,
            completion_tokens=200,
            cost_usd=0.0,
        )
    rows = await usage.for_day(NOW.date())
    assert len(rows) == 1
    assert rows[0]["requests"] == 2 and rows[0]["prompt_tokens"] == 2000
