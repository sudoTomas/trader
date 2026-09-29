"""Breakers, approvals, and the RiskGuard shell — against the real compose Postgres.

These cover what the pure chain can't: state that must survive a restart, the approval
lifecycle, and the audit trail.
"""

from __future__ import annotations

import ast
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trader.core.symbols import default_registry
from trader.core.types import OrderIntent, Side
from trader.risk.approvals import ApprovalService
from trader.risk.breakers import DAILY_LOSS, WEEKLY_DRAWDOWN, BreakerStore
from trader.risk.guard import AccountState, RiskDenied, RiskGuard
from trader.risk.limits import RiskLimits
from trader.store.db import Database

BTC = default_registry().get("BTC/USD")
NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
DAY = date(2026, 8, 12)

LIMITS = RiskLimits(
    max_position_frac=0.05,
    min_notional_usd=5.0,
    max_concurrent_positions=3,
    max_trades_per_day=5,
    daily_loss_limit_frac=0.03,
    weekly_drawdown_limit_frac=0.10,
    approval_notional_usd=1_000.0,
)


async def observe(store: BreakerStore, equity: float, day: date = DAY):
    return await store.observe(
        equity=equity,
        session_day=day,
        daily_loss_limit_frac=LIMITS.daily_loss_limit_frac,
        weekly_drawdown_limit_frac=LIMITS.weekly_drawdown_limit_frac,
    )


# --- breakers ---------------------------------------------------------------


async def test_daily_loss_breaker_trips_and_blocks(db: Database):
    store = BreakerStore(db)
    state = await observe(store, 100_000.0)
    assert state.halted_reason is None
    assert state.day_start_equity == 100_000.0

    assert (await observe(store, 97_500.0)).halted_reason is None  # -2.5%, inside the limit
    tripped = await observe(store, 96_900.0)  # -3.1%
    assert tripped.halted_reason == DAILY_LOSS
    assert "daily_loss" in tripped.blocked_reason


async def test_weekly_drawdown_measured_off_the_high_water(db: Database):
    store = BreakerStore(db)
    await observe(store, 100_000.0)
    await observe(store, 120_000.0)  # new high-water
    # -9% off the high is fine; the day window was re-based at 100k so it doesn't trip.
    assert (await observe(store, 109_200.0)).halted_reason is None
    assert (await observe(store, 107_000.0)).halted_reason == WEEKLY_DRAWDOWN


async def test_breaker_state_survives_restart(db: Database):
    await observe(BreakerStore(db), 100_000.0)
    await observe(BreakerStore(db), 90_000.0)  # trips daily loss
    # A brand-new store instance = a restarted process. The halt must still be there.
    assert (await BreakerStore(db).load()).halted_reason == DAILY_LOSS


async def test_resume_rebaselines_so_it_does_not_instantly_retrip(db: Database):
    """v1 lesson: a monotonic high-water re-trips the kill on the very next tick."""
    store = BreakerStore(db)
    await observe(store, 100_000.0)
    await observe(store, 85_000.0)  # trips both windows
    assert (await store.load()).halted_reason is not None

    await store.resume(equity=85_000.0)
    state = await observe(store, 85_000.0)
    assert state.halted_reason is None  # stays clear at the new baseline
    assert state.week_high_equity == 85_000.0


async def test_new_session_day_clears_a_daily_halt(db: Database):
    store = BreakerStore(db)
    await observe(store, 100_000.0)
    await observe(store, 96_000.0)  # -4% in one day
    assert (await store.load()).halted_reason == DAILY_LOSS
    assert (await observe(store, 96_000.0, day=DAY + timedelta(days=1))).halted_reason is None


async def test_slow_bleed_trips_drawdown_and_a_new_day_does_not_clear_it(db: Database):
    """Each new day re-bases the daily window, so a slow bleed never trips daily loss —
    the drawdown breaker is what catches it, and a date change must not release it."""
    store = BreakerStore(db)
    await observe(store, 100_000.0)  # high-water 100k
    for offset, equity in enumerate((98_000.0, 95_000.0, 91_000.0), start=1):
        assert (await observe(store, equity, day=DAY + timedelta(days=offset))).halted_reason is (
            None
        )
    tripped = await observe(store, 89_000.0, day=DAY + timedelta(days=4))  # -11% off the high
    assert tripped.halted_reason == WEEKLY_DRAWDOWN
    # A new day does NOT clear a drawdown halt — it isn't a daily concept.
    assert (await observe(store, 89_000.0, day=DAY + timedelta(days=5))).halted_reason == (
        WEEKLY_DRAWDOWN
    )


async def test_pause_and_drift_are_persisted(db: Database):
    store = BreakerStore(db)
    await store.pause("operator maintenance")
    assert (await store.load()).blocked_reason.startswith("trading paused")
    await store.unpause()
    assert (await store.load()).blocked_reason is None

    await store.set_drift(True)
    assert "drift" in (await store.load()).blocked_reason


# --- approvals --------------------------------------------------------------


async def test_approval_round_trip(db: Database):
    service = ApprovalService(db)
    intent = OrderIntent("BTC/USD", Side.BUY, notional=5_000.0)
    approval_id = await service.park(intent, price=60_000.0, reason="over threshold")

    assert [r["id"] for r in await service.pending()] == [approval_id]
    assert await service.resolve(approval_id, approved=True, resolved_by="tomas")

    redemption = await service.redeem(
        approval_id,
        current_price=60_100.0,  # +0.17%, inside tolerance
        ttl_seconds=LIMITS.approval_ttl_seconds,
        price_tolerance_frac=LIMITS.approval_price_tolerance_frac,
    )
    assert redemption.ok
    assert redemption.intent == intent


async def test_stale_approval_is_refused(db: Database):
    """An approval granted against a stale quote must not become a fill — that is how
    v1 produced stale fills (see docs/lessons.md#approval-price-tolerance)."""
    service = ApprovalService(db)
    intent = OrderIntent("BTC/USD", Side.BUY, notional=5_000.0)
    approval_id = await service.park(intent, price=60_000.0, reason="over threshold")
    await service.resolve(approval_id, approved=True, resolved_by="tomas")

    moved = await service.redeem(
        approval_id,
        current_price=63_000.0,  # +5%, well outside tolerance
        ttl_seconds=LIMITS.approval_ttl_seconds,
        price_tolerance_frac=LIMITS.approval_price_tolerance_frac,
    )
    assert not moved.ok and "price moved" in moved.reason

    expired = await service.redeem(
        approval_id,
        current_price=60_000.0,
        ttl_seconds=LIMITS.approval_ttl_seconds,
        price_tolerance_frac=LIMITS.approval_price_tolerance_frac,
        now=datetime.now(tz=UTC) + timedelta(hours=2),
    )
    assert not expired.ok and "expired" in expired.reason


async def test_unapproved_and_denied_rows_never_redeem(db: Database):
    service = ApprovalService(db)
    intent = OrderIntent("BTC/USD", Side.BUY, notional=5_000.0)
    pending_id = await service.park(intent, price=60_000.0, reason="over threshold")
    denied_id = await service.park(intent, price=60_000.0, reason="over threshold")
    await service.resolve(denied_id, approved=False, resolved_by="tomas")

    kwargs = {
        "current_price": 60_000.0,
        "ttl_seconds": LIMITS.approval_ttl_seconds,
        "price_tolerance_frac": LIMITS.approval_price_tolerance_frac,
    }
    assert not (await service.redeem(pending_id, **kwargs)).ok
    assert not (await service.redeem(denied_id, **kwargs)).ok
    assert not (await service.redeem(999_999, **kwargs)).ok


# --- guard shell ------------------------------------------------------------


async def guard_eval(db: Database, intent: OrderIntent, **kwargs):
    guard = RiskGuard(db, LIMITS)
    account = kwargs.pop("account", AccountState(equity=100_000.0))
    return await guard.evaluate(
        intent, BTC, price=60_000.0, account=account, now=NOW, realized_vol=0.02, **kwargs
    )


async def test_guard_approves_clamps_and_audits(db: Database):
    outcome = await guard_eval(db, OrderIntent("BTC/USD", Side.BUY, notional=50_000.0))
    # 5% position cap → 5_000, then the unattended approval cap clamps to 1_000.
    assert outcome.approved
    assert not outcome.approval_required
    assert outcome.approval_id is None
    assert outcome.executable().notional == pytest.approx(1_000.0)

    rows = await db.fetch_all("SELECT * FROM audit_events WHERE category = 'risk'")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["approved"] is True
    assert payload["requested"]["notional"] == 50_000.0
    assert [v["check"] for v in payload["verdicts"]][:2] == ["input_sanity", "paper_gate"]
    assert payload["price"] == 60_000.0
    approval = next(v for v in payload["verdicts"] if v["check"] == "approval_threshold")
    assert approval["action"] == "clamp"
    assert approval["notional"] == 1_000.0


async def test_guard_parks_only_above_an_explicit_park_band(db: Database):
    guard = RiskGuard(db, replace(LIMITS, park_above_usd=2_000.0))
    outcome = await guard.evaluate(
        OrderIntent("BTC/USD", Side.BUY, notional=3_000.0),
        BTC,
        price=60_000.0,
        account=AccountState(equity=100_000.0),
        now=NOW,
        realized_vol=0.02,
    )
    assert outcome.approval_required and not outcome.approved
    parked = await ApprovalService(db).pending()
    assert [r["id"] for r in parked] == [outcome.approval_id]
    assert parked[0]["order_intent"]["parked_price"] == 60_000.0
    assert parked[0]["order_intent"]["notional"] == 3_000.0
    with pytest.raises(RiskDenied):
        outcome.executable()

    # Same size with the band off clamps to the cap and does not join the queue.
    clamped = await guard_eval(db, OrderIntent("BTC/USD", Side.BUY, notional=3_000.0))
    assert clamped.approved
    assert clamped.executable().notional == pytest.approx(1_000.0)
    assert [r["id"] for r in await ApprovalService(db).pending()] == [outcome.approval_id]


async def test_shadow_does_not_park_the_band(db: Database):
    guard = RiskGuard(db, replace(LIMITS, park_above_usd=2_000.0))
    outcome = await guard.evaluate(
        OrderIntent("BTC/USD", Side.BUY, notional=3_000.0),
        BTC,
        price=60_000.0,
        account=AccountState(equity=100_000.0),
        now=NOW,
        realized_vol=0.02,
        shadow=True,
    )
    assert outcome.approval_required and outcome.approval_id is None
    assert await ApprovalService(db).pending() == []


async def test_guard_denies_when_a_breaker_is_already_tripped(db: Database):
    store = BreakerStore(db)
    await observe(store, 100_000.0)
    outcome = await guard_eval(
        db,
        OrderIntent("BTC/USD", Side.BUY, notional=500.0),
        account=AccountState(equity=90_000.0),  # -10% → trips daily loss inside evaluate()
    )
    assert not outcome.approved
    assert outcome.verdicts[-1].check == "breaker"


async def test_guard_lets_an_exit_through_a_tripped_breaker(db: Database):
    store = BreakerStore(db)
    await observe(store, 100_000.0)
    outcome = await guard_eval(
        db,
        OrderIntent("BTC/USD", Side.SELL, qty=0.01),
        account=AccountState(equity=90_000.0, positions={"BTC/USD": 0.5}),
    )
    assert outcome.approved
    assert outcome.executable().qty == 0.01


async def test_guard_is_paper_only_by_default(db: Database):
    denied = await guard_eval(db, OrderIntent("BTC/USD", Side.BUY, notional=500.0), live=True)
    assert not denied.approved
    assert denied.verdicts[-1].check == "paper_gate"

    enabled = RiskGuard(db, LIMITS, live_enabled=True)
    allowed = await enabled.evaluate(
        OrderIntent("BTC/USD", Side.BUY, notional=500.0),
        BTC,
        price=60_000.0,
        account=AccountState(equity=100_000.0),
        now=NOW,
        realized_vol=0.02,
        live=True,
    )
    assert allowed.approved


async def test_guard_denies_unreadable_input_and_still_audits_it(db: Database):
    """A NaN order size must deny AND record — Postgres JSON has no NaN, so an
    unsanitized audit write would crash on exactly the input worth recording."""
    outcome = await guard_eval(db, OrderIntent("BTC/USD", Side.BUY, qty=math.nan))
    assert not outcome.approved
    assert outcome.verdicts[-1].check == "input_sanity"
    with pytest.raises(RiskDenied):
        outcome.executable()

    row = await db.fetch_one("SELECT payload FROM audit_events WHERE category = 'risk'")
    assert row is not None
    assert row["payload"]["requested"]["qty"] == "NaN"  # preserved, not silently nulled


# --- advisory firewall ------------------------------------------------------


def test_riskguard_never_reads_research_or_the_macro_view():
    """Structural firewall: research shapes direction/conviction, never sizing or
    breakers (docs/architecture.md §Operator Research Desk).

    Checked over the AST — real imports and identifiers, so prose about the firewall
    (in docstrings, like this one) can't accidentally satisfy or break it.
    """
    risk_dir = Path(__file__).resolve().parents[1] / "src" / "trader" / "risk"
    sources = list(risk_dir.glob("*.py"))
    assert sources, "risk package not found"

    forbidden_identifiers = {"macro_view", "MacroView", "macro_views", "research_notes"}
    for path in sources:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                if isinstance(node, ast.Name):
                    assert node.id not in forbidden_identifiers, f"{path.name}: {node.id}"
                elif isinstance(node, ast.Attribute):
                    assert node.attr not in forbidden_identifiers, f"{path.name}: {node.attr}"
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # SQL strings must not reach research tables either.
                    lowered = node.value.lower()
                    if "select" in lowered or "from " in lowered:
                        assert "macro_views" not in lowered and "research_notes" not in lowered
                continue
            for module in modules:
                assert not module.startswith("trader.research"), f"{path.name} imports {module}"
