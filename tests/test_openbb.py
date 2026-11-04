"""OpenBB sidecar (#141): the producer, its three Sources, and the earnings blackout.

The sidecar is never reached: ``httpx.MockTransport`` serves recorded OBBject bodies
(``tests/golden/openbb_aapl.json``). Seams: ``OpenBBProducer.refresh`` (rows written),
the Sources' ``read`` and the decide prompt (what a Bot sees, pinned to ``as_of``),
``evaluate_chain`` / ``RiskGuard.evaluate`` (the ``earnings_blackout`` verdict), the
replay's crude book, and the service registry.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_loop import BTC, NOW, SPY, StubLLM, StubMarketData
from tests.test_risk_chain import LIMITS, checks, ctx, denied_by
from trader.core.symbols import AssetClass, make_symbol
from trader.core.types import OrderIntent, Side
from trader.data.openbb import OpenBBClient, records
from trader.loop.context import ContextBuilder
from trader.loop.sources import EARNINGS, FUNDAMENTALS, OPTIONS, build_sources
from trader.research.openbb import BREAKER_THRESHOLD, OpenBBProducer
from trader.risk.earnings import EarningsCalendar
from trader.risk.guard import AccountState, RiskGuard, evaluate_chain
from trader.store.db import Database
from trader.store.repos import OpenBBRepo

AAPL = make_symbol("AAPL", AssetClass.US_EQUITY)
FIXTURES = json.loads((Path(__file__).parent / "golden" / "openbb_aapl.json").read_text())


class Sidecar:
    """A recorded openbb-api: answers AAPL from the fixtures, 204 for anything else."""

    def __init__(self, *, down: set[str] = frozenset(), unreachable: bool = False) -> None:
        self.down = set(down)  # providers that answer 502
        self.unreachable = unreachable
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        route = request.url.path.removeprefix("/api/v1/")
        provider = request.url.params.get("provider", "")
        self.calls.append((route, provider))
        if provider in self.down:
            return httpx.Response(502, json={"detail": "upstream page changed"})
        symbol = request.url.params.get("symbol")
        if route == "derivatives/options/chains" and provider == "nasdaq":
            provider = "cboe"  # the fallback serves the same recorded chain
        body = FIXTURES.get(f"{route}/{provider}")
        if body is None or (symbol is not None and symbol != "AAPL"):
            return httpx.Response(204)
        return httpx.Response(200, json=body)

    def client(self) -> OpenBBClient:
        transport = httpx.MockTransport(self.handler)
        return OpenBBClient(httpx.AsyncClient(base_url="http://openbb", transport=transport))


def producer(db: Database, sidecar: Sidecar, *, now: datetime = NOW) -> OpenBBProducer:
    return OpenBBProducer(db, sidecar.client(), clock=lambda: now)


# --- the client ------------------------------------------------------------------------


def test_records_reads_every_results_shape():
    assert records(None) == []
    assert records([{"a": 1}]) == [{"a": 1}]
    assert records({"a": 1, "b": "x"}) == [{"a": 1, "b": "x"}]
    columnar = {"strike": [1.0, 2.0], "option_type": ["call", "put"]}
    assert records(columnar) == [
        {"strike": 1.0, "option_type": "call"},
        {"strike": 2.0, "option_type": "put"},
    ]


# --- the producer ----------------------------------------------------------------------


async def test_a_refresh_writes_one_row_per_kind_from_the_recorded_answers(db: Database):
    out = await producer(db, Sidecar()).refresh(["AAPL", "MSFT"])

    assert out["status"] == "ok"
    assert out["written"] == {
        EARNINGS: ["AAPL", "MSFT"],
        OPTIONS: ["AAPL"],
        FUNDAMENTALS: ["AAPL"],
    }
    repo = OpenBBRepo(db)
    earnings = (await repo.latest(EARNINGS, "AAPL"))["data"]
    assert earnings["next_report_date"] == "2026-09-10"
    assert earnings["report_time"] == "before market open"
    assert earnings["eps_consensus"] == 1.62
    assert earnings["num_estimates"] == 25
    assert earnings["last_report"] == {
        "date": "2026-07-30",
        "eps_actual": 1.57,
        "eps_estimated": 1.43,
        "surprise_frac": 0.0979,
    }
    # No report in the window is a fact too: MSFT's row says so rather than going missing.
    msft = (await repo.latest(EARNINGS, "MSFT"))["data"]
    assert msft["next_report_date"] is None
    assert msft["window_end"] == "2026-09-26"

    options = (await repo.latest(OPTIONS, "AAPL"))["data"]
    assert options["expiration"] == "2026-08-14"  # the 0DTE expiry is skipped
    assert options["atm_strike"] == 230.0
    assert options["atm_iv"] == pytest.approx(0.29)
    assert options["implied_move_frac"] == pytest.approx(7.5 / 230.0)  # straddle 4.0 + 3.5
    assert options["put_call_skew"] == pytest.approx(0.34 - 0.26)  # 220 put vs 240 call

    fundamentals = await repo.latest(FUNDAMENTALS, "AAPL")
    assert fundamentals["provider"] == "nasdaq,sec,finra"
    data = fundamentals["data"]
    assert data["price_target"]["consensus"] == 255.5
    assert data["ratios"] == {
        "period_ending": "2026-06-30",
        "net_profit_margin": 0.24,
        "return_on_equity": 1.5,
    }
    # The March sale is outside the 90-day window.
    assert data["insider"] == {"window_days": 90, "buys": 1, "sells": 1, "net_shares": -9500.0}
    assert data["short_interest"]["settlement_date"] == "2026-07-31"
    assert data["short_interest"]["change_frac"] == pytest.approx(0.0526)


async def test_without_a_sidecar_url_a_refresh_is_unavailable(db: Database):
    out = await OpenBBProducer(db, None).refresh(["AAPL"])
    assert out["status"] == "unavailable"
    assert await OpenBBRepo(db).recent() == []


async def test_an_unreachable_sidecar_writes_nothing_and_charges_no_provider(db: Database):
    prod = producer(db, Sidecar(unreachable=True))
    out = await prod.refresh(["AAPL"])
    assert out["status"] == "unavailable"
    assert "unreachable" in out["reason"]
    assert await OpenBBRepo(db).recent() == []
    assert all(b.failures == 0 for b in prod.breakers.values())


async def test_a_failed_calendar_writes_no_earnings_row(db: Database):
    out = await producer(db, Sidecar(down={"nasdaq"})).refresh(["AAPL"])
    assert out["written"][EARNINGS] == []
    assert await OpenBBRepo(db).latest(EARNINGS, "AAPL") is None
    # The other providers still answered: options from Cboe, a partial fundamentals note.
    assert out["written"][OPTIONS] == ["AAPL"]
    assert (await OpenBBRepo(db).latest(FUNDAMENTALS, "AAPL"))["provider"] == "sec,finra"


async def test_a_broken_provider_trips_its_breaker_and_the_fallback_serves(db: Database):
    sidecar = Sidecar(down={"cboe"})
    prod = producer(db, sidecar)
    for _ in range(BREAKER_THRESHOLD):
        out = await prod.refresh(["AAPL"])
        assert out["written"][OPTIONS] == ["AAPL"]  # Nasdaq's chain stood in
    assert prod.breakers["cboe"].open_until is not None
    assert prod.breakers["nasdaq"].failures == 0

    sidecar.calls.clear()
    out = await prod.refresh(["AAPL"])
    assert "cboe" in out["skipped_providers"]
    assert ("derivatives/options/chains", "cboe") not in sidecar.calls
    assert (await OpenBBRepo(db).latest(OPTIONS, "AAPL"))["provider"] == "nasdaq"


async def test_the_breaker_closes_again_after_its_cooldown(db: Database):
    sidecar = Sidecar(down={"cboe"})
    clock = {"now": NOW}
    prod = OpenBBProducer(db, sidecar.client(), clock=lambda: clock["now"])
    for _ in range(BREAKER_THRESHOLD):
        await prod.refresh(["AAPL"])
    sidecar.down.clear()
    clock["now"] = NOW + timedelta(hours=7)
    out = await prod.refresh(["AAPL"])
    assert "cboe" not in out["skipped_providers"]
    assert prod.breakers["cboe"].failures == 0


# --- the Sources -----------------------------------------------------------------------


async def seed_rows(db: Database, *, at: datetime) -> None:
    await producer(db, Sidecar(), now=at).refresh(["AAPL"])
    await db.execute("UPDATE openbb_snapshots SET ts = %s", (at,))


async def test_each_source_renders_a_dated_note(db: Database):
    await seed_rows(db, at=NOW - timedelta(hours=3))
    sources = build_sources(db, StubMarketData())
    params = {"now": NOW}

    earnings = await sources[EARNINGS].read(AAPL, as_of=None, params=params)
    assert earnings is not None and isinstance(earnings.version, int)
    assert "Fetched 2026-08-12T12:00:00+00:00 (3h before this read) via OpenBB/nasdaq" in (
        earnings.text
    )
    assert "Next earnings report: 2026-09-10, before market open (29 calendar days away);" in (
        earnings.text
    )
    assert "consensus EPS 1.62 (25 estimates)" in earnings.text
    assert "Last report 2026-07-30: EPS 1.57 vs 1.43 estimated (+9.8% surprise)" in earnings.text
    assert "STALE" not in earnings.text

    options = await sources[OPTIONS].read(AAPL, as_of=None, params=params)
    assert "Options, 2026-08-14 expiry (2d): ATM implied vol 29.0%" in options.text
    assert "implied move ±3.3% by expiry" in options.text
    assert "put/call skew +8.0 vol pts" in options.text

    fundamentals = await sources[FUNDAMENTALS].read(AAPL, as_of=None, params=params)
    for line in (
        "Price target consensus 255.50 (low 200.00, high 300.00); rating Buy",
        "Ratios (period ending 2026-06-30): net margin 24.0%, ROE 150.0%",
        "Insider trades (90d): 1 buys, 1 sells, net -9,500 shares",
        "Short interest (settled 2026-07-31): 120,000,000 shares, 1.8 days to cover",
    ):
        assert line in fundamentals.text


async def test_a_pinned_read_never_sees_a_row_fetched_after_it(db: Database):
    await seed_rows(db, at=NOW)
    sources = build_sources(db, StubMarketData())
    before = NOW - timedelta(minutes=1)
    for name in (EARNINGS, OPTIONS, FUNDAMENTALS):
        assert await sources[name].read(AAPL, as_of=before, params={"now": before}) is None
        assert await sources[name].read(AAPL, as_of=NOW, params={"now": NOW}) is not None


async def test_an_old_row_is_flagged_stale_and_a_passed_report_says_so(db: Database):
    await seed_rows(db, at=NOW - timedelta(days=40))
    later = NOW + timedelta(days=30)  # past the 2026-09-10 report
    earnings = await build_sources(db, StubMarketData())[EARNINGS].read(
        AAPL, as_of=later, params={"now": later}
    )
    assert "STALE: older than 3d" in earnings.text
    assert "Last scheduled report 2026-09-10 has passed" in earnings.text


async def test_a_subscribed_bot_reads_the_lanes_and_journals_their_versions(db: Database):
    await seed_rows(db, at=NOW - timedelta(hours=1))
    builder = ContextBuilder(
        StubMarketData(),
        sources=build_sources(db, StubMarketData()),
        subscriptions=[{"source": EARNINGS}, {"source": OPTIONS}, {"source": FUNDAMENTALS}],
    )
    context = await builder.build(AAPL, now=NOW, triggers=[])
    prompt = context.to_prompt()
    assert "Earnings calendar (advisory" in prompt
    assert "Options market (advisory" in prompt
    assert "Fundamentals note (advisory" in prompt
    assert set(context.source_versions) == {EARNINGS, OPTIONS, FUNDAMENTALS}

    # Unsubscribed (every Bot by default): no lane, nothing journaled.
    unsubscribed = ContextBuilder(StubMarketData(), sources=build_sources(db, StubMarketData()))
    bare = await unsubscribed.build(AAPL, now=NOW, triggers=[])
    assert "Earnings calendar" not in bare.to_prompt()
    assert bare.source_versions == {}


def test_a_bot_may_subscribe_to_the_openbb_sources(monkeypatch):
    from trader.loop.bots import bot_spec
    from trader.ops.config import Settings

    monkeypatch.setattr("trader.ops.config._settings", Settings(extra_symbols="AAPL"))
    spec = bot_spec(symbols={"AAPL": "shadow"}, capital_usd=5_000, sources=["earnings", "options"])
    assert [s["source"] for s in spec["sources"]] == ["earnings", "options"]


# --- the earnings blackout: the pure chain --------------------------------------------

BLACKOUT = replace(LIMITS, earnings_blackout_days=1)
FRESH = NOW - timedelta(hours=2)
SPY_HELD = AccountState(equity=100_000.0, positions={"SPY": 5.0})


def report(day: date, *, fetched: datetime = FRESH) -> EarningsCalendar:
    return EarningsCalendar(version=7, fetched_at=fetched, report_date=day)


def spy(**overrides: Any):
    base = {
        "intent": OrderIntent("SPY", Side.BUY, notional=500.0),
        "symbol": SPY,
        "price": 500.0,
        "limits": BLACKOUT,
    }
    return ctx(**{**base, **overrides})


@pytest.mark.parametrize(
    "day",
    [date(2026, 8, 13), date(2026, 8, 12)],  # NOW is Wed 2026-08-12: the session before, the day
    ids=["session-before", "report-session"],
)
def test_opens_are_blacked_out_the_session_before_and_the_report_session(day):
    outcome = evaluate_chain(spy(earnings=report(day)))
    assert denied_by(outcome) == "earnings_blackout"
    assert day.isoformat() in outcome.reason
    assert "calendar #7" in outcome.reason


def test_an_add_is_blacked_out_too():
    outcome = evaluate_chain(spy(earnings=report(date(2026, 8, 13)), account=SPY_HELD))
    assert denied_by(outcome) == "earnings_blackout"


@pytest.mark.parametrize("day", [date(2026, 8, 14), date(2026, 8, 11)], ids=["later", "passed"])
def test_outside_the_window_opens_pass(day):
    outcome = evaluate_chain(spy(earnings=report(day)))
    assert outcome.approved
    assert "earnings_blackout" in checks(outcome)


def test_the_window_counts_sessions_not_days():
    """Friday before a Monday report is the session before it."""
    friday = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
    outcome = evaluate_chain(
        spy(now=friday, earnings=report(date(2026, 8, 17), fetched=friday - timedelta(hours=1)))
    )
    assert denied_by(outcome) == "earnings_blackout"
    # Two sessions out with days=2 is inside; with days=1 it is not.
    two_out = report(date(2026, 8, 14))
    assert evaluate_chain(spy(earnings=two_out)).approved
    wider = replace(BLACKOUT, earnings_blackout_days=2)
    assert denied_by(evaluate_chain(spy(earnings=two_out, limits=wider))) == "earnings_blackout"


@pytest.mark.parametrize("notional", [100.0, 2_500.0])
def test_closes_and_trims_always_pass(notional):
    trim = OrderIntent("SPY", Side.SELL, notional=notional)
    outcome = evaluate_chain(spy(intent=trim, account=SPY_HELD, earnings=report(date(2026, 8, 12))))
    assert outcome.approved
    assert "earnings_blackout" not in checks(outcome)


@pytest.mark.parametrize(
    "earnings",
    [None, report(date(2026, 8, 13), fetched=NOW - timedelta(days=4))],
    ids=["no-calendar", "stale-calendar"],
)
def test_no_usable_calendar_fails_open_and_the_verdict_says_so(earnings):
    outcome = evaluate_chain(spy(earnings=earnings))
    assert outcome.approved
    verdict = next(v for v in outcome.verdicts if v.check == "earnings_blackout")
    assert verdict.action == "allow"
    assert "blackout not applied" in verdict.reason


def test_zero_days_turns_the_blackout_off_and_crypto_never_reports():
    off = evaluate_chain(spy(earnings=report(date(2026, 8, 12)), limits=LIMITS))
    assert off.approved and "earnings_blackout" not in checks(off)
    crypto = evaluate_chain(ctx(limits=BLACKOUT))
    assert crypto.approved and "earnings_blackout" not in checks(crypto)
    assert BTC.asset_class is AssetClass.CRYPTO


def test_the_setting_reaches_the_limits():
    from trader.ops.config import Settings
    from trader.risk.limits import RiskLimits, limits_from_settings

    assert limits_from_settings(Settings()).earnings_blackout_days == 1
    assert limits_from_settings(Settings(risk_earnings_blackout_days=0)).earnings_blackout_days == 0
    with pytest.raises(ValueError, match="earnings_blackout_days"):
        RiskLimits(earnings_blackout_days=-1)


# --- the earnings blackout: RiskGuard.evaluate reads the producer's row ----------------


async def test_the_guard_reads_the_calendar_row_and_journals_its_verdict(db: Database):
    repo = OpenBBRepo(db)
    await repo.insert(
        kind=EARNINGS,
        symbol="SPY",
        provider="nasdaq",
        data={"next_report_date": "2026-08-13"},
        ts=FRESH,
    )
    guard = RiskGuard(db, BLACKOUT)
    account = AccountState(equity=100_000.0)
    outcome = await guard.evaluate(
        OrderIntent("SPY", Side.BUY, notional=500.0), SPY, price=500.0, account=account, now=NOW
    )
    assert denied_by(outcome) == "earnings_blackout"
    audit = await db.fetch_one("SELECT payload FROM audit_events WHERE category = 'risk'")
    assert audit["payload"]["verdicts"][-1]["check"] == "earnings_blackout"

    # A row fetched after the decision's clock is invisible to it: the guard fails open.
    await db.execute("UPDATE openbb_snapshots SET ts = %s", (NOW + timedelta(minutes=5),))
    later_row = await guard.evaluate(
        OrderIntent("SPY", Side.BUY, notional=500.0), SPY, price=500.0, account=account, now=NOW
    )
    assert later_row.approved


# --- replay: the crude book honors the blackout, point in time --------------------------


async def test_replay_blacks_out_an_open_once_the_calendar_row_exists(db: Database):
    from tests.test_replay_eval import OPEN
    from trader.eval.replay import ReplayHarness
    from trader.store.repos import BarsRepo

    await BarsRepo(db).upsert_many(
        [
            {
                "symbol": "SPY",
                "interval": "1h",
                "ts": NOW - timedelta(hours=60 - i),
                "open": 500.0 + i,
                "high": 501.0 + i,
                "low": 499.0 + i,
                "close": 500.0 + i,
                "volume": 1_000_000.0,
            }
            for i in range(60)
        ]
    )
    await OpenBBRepo(db).insert(
        kind=EARNINGS,
        symbol="SPY",
        provider="nasdaq",
        data={"next_report_date": "2026-08-13"},
        ts=NOW - timedelta(minutes=30),
    )
    limits = replace(BLACKOUT, max_position_frac=0.5)

    # 10:00 ET: the calendar row is not fetched yet, so the open goes (fails open).
    early = ReplayHarness(db, StubLLM([OPEN]), StubMarketData(), limits=limits)
    run = await early.run(SPY, NOW - timedelta(hours=1), NOW, label="before")
    assert run["summary"]["earnings_blackout_denied"] == 0

    # 11:00 ET: it is, and the report is tomorrow.
    late = ReplayHarness(db, StubLLM([OPEN]), StubMarketData(), limits=limits)
    run = await late.run(SPY, NOW, NOW + timedelta(hours=1), label="after")
    assert run["summary"]["earnings_blackout_denied"] == 1
    step = await db.fetch_one("SELECT decision FROM eval_steps WHERE run_id = %s", (run["id"],))
    assert step["decision"]["earnings_blackout"] is True
    assert "fill_cost" not in step["decision"]


# --- the service layer -----------------------------------------------------------------


async def test_refresh_defaults_to_every_bots_us_stocks_and_is_audited(db: Database, monkeypatch):
    from trader.loop.bots import BotDesk, bot_spec
    from trader.ops.config import Settings
    from trader.surfaces.services import build_registry

    monkeypatch.setattr("trader.ops.config._settings", Settings(extra_symbols="AAPL"))
    await BotDesk(db).create(
        "bot-2", bot_spec(symbols={"AAPL": "shadow", "BTC/USD": "shadow"}, capital_usd=5_000)
    )
    registry = build_registry(db=db, openbb=producer(db, Sidecar()))

    out = await registry.get("refresh_openbb").handler()
    assert out["status"] == "ok"
    assert out["written"][EARNINGS] == ["AAPL"]  # the crypto pair is not refreshed
    audit = await db.fetch_one("SELECT payload FROM audit_events WHERE action = 'refresh_openbb'")
    assert audit["payload"]["symbols"] == ["AAPL"]

    rows = await registry.get("openbb").handler(symbol="aapl", kind=OPTIONS)
    assert [r["kind"] for r in rows] == [OPTIONS]
    assert isinstance(rows[0]["ts"], str)


async def test_refresh_without_a_producer_is_unavailable(db: Database):
    from trader.surfaces.services import build_registry

    out = await build_registry(db=db).get("refresh_openbb").handler(symbols="AAPL")
    assert out["status"] == "unavailable"


def test_every_door_reads_and_refreshes_the_same(clean_db: None, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app
    from trader.surfaces.services import build_registry

    async def seed() -> None:
        database = Database(min_size=1, max_size=1)
        await database.open()
        try:
            await OpenBBRepo(database).insert(
                kind=EARNINGS, symbol="AAPL", provider="nasdaq", data={"next_report_date": None}
            )
        finally:
            await database.close()

    asyncio.run(seed())
    boot = Settings(auth_disabled=True)  # no TRADER_OPENBB_BASE_URL: no producer client
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(boot)))
    with TestClient(create_app(boot)) as tc:
        api_rows = tc.get("/research/openbb", params={"symbol": "AAPL"}).json()
        api_refresh = tc.post("/research/openbb/refresh", params={"symbols": "AAPL"}).json()

    shown = CliRunner().invoke(cli.app, ["openbb", "show", "--symbol", "AAPL"])
    refreshed = CliRunner().invoke(cli.app, ["openbb", "refresh", "--symbols", "AAPL"])
    assert shown.exit_code == 0 and refreshed.exit_code == 0, (shown.output, refreshed.output)
    assert json.loads(shown.output) == api_rows and len(api_rows) == 1
    assert json.loads(refreshed.output) == api_refresh
    assert api_refresh == {"status": "unavailable", "reason": "TRADER_OPENBB_BASE_URL is not set"}

    # MCP tools are generated from the same registry.
    assert {"get_openbb", "refresh_openbb"} <= {c.tool_name for c in build_registry().all()}
