"""EAI-188: replay eval harness — rebuild historical context from the bars cache, rerun
the decider offline, journal to eval_runs/eval_steps (NEVER decisions), score crudely.

The LLM is stubbed; the store is real. This harness is the gate-keeper for every future
decide-behavior change (docs/runbooks/decide-change-gate.md).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from tests.test_loop import StubLLM, StubMarketData
from trader.core.symbols import AssetClass, default_registry, make_symbol
from trader.data.bars_cache import BarsCache
from trader.eval.replay import ReplayHarness, ReplayMarketData
from trader.loop.context import ContextBuilder
from trader.loop.scheduler import Trigger
from trader.store.db import Database
from trader.store.repos import BarsRepo

BTC = default_registry().get("BTC/USD")
SPY = make_symbol("SPY", AssetClass.US_EQUITY)
NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)  # Wednesday 11:00 ET
SATURDAY = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

HOLD = '{"action":"hold","conviction":0.3,"rationale":"nothing doing"}'
OPEN = '{"action":"open","conviction":0.8,"rationale":"momentum","side":"buy","notional_usd":1000}'
CLOSE = (
    '{"action":"close","conviction":0.7,"rationale":"take profit",'
    '"side":"sell","notional_usd":1000}'
)


async def seed_hourly_bars(db: Database, *, count: int = 60, base: float = 60_000.0) -> None:
    """Rising hourly closes ending one hour before NOW (ts is the bar's open time)."""
    await BarsRepo(db).upsert_many(
        [
            {
                "symbol": "BTC/USD",
                "interval": "1h",
                "ts": NOW - timedelta(hours=count - i),
                "open": base + i * 10,
                "high": base + i * 10 + 5,
                "low": base + i * 10 - 5,
                "close": base + i * 10,
                "volume": 10.0,
            }
            for i in range(count)
        ]
    )


def cache(db: Database, provider=None) -> BarsCache:
    return BarsCache(db, provider or StubMarketData())


# --- ReplayMarketData --------------------------------------------------------


async def test_replay_market_data_serves_bars_strictly_before_as_of(db: Database):
    await seed_hourly_bars(db)
    md = ReplayMarketData(cache(db))
    md.as_of = NOW - timedelta(hours=10)

    bars = await md.get_bars(BTC, "1h", 20)
    assert len(bars) == 20
    assert bars == sorted(bars, key=lambda b: b.ts)
    assert all(b.ts < md.as_of for b in bars)  # the future never leaks into a replay

    quote = await md.get_latest_quote(BTC)
    assert quote.mid == bars[-1].close  # both sides quoted at the last completed close


async def test_prompt_rebuild_is_deterministic_for_same_inputs(db: Database):
    """The checklist's byte-equality clause: same inputs → identical prompt, twice."""
    await seed_hourly_bars(db)
    md = ReplayMarketData(cache(db))
    md.as_of = NOW - timedelta(hours=5)
    builder = ContextBuilder(md)

    kwargs = dict(now=md.as_of, triggers=[Trigger("replay", "BTC/USD")], equity=100_000.0)
    first = (await builder.build(BTC, **kwargs)).to_prompt()
    second = (await builder.build(BTC, **kwargs)).to_prompt()
    assert first == second
    assert "Interval 1h" in first


# --- the harness -------------------------------------------------------------


async def test_run_journals_eval_rows_and_never_decisions(db: Database):
    await seed_hourly_bars(db)
    harness = ReplayHarness(db, StubLLM([OPEN, HOLD, CLOSE]), StubMarketData())

    run = await harness.run(
        BTC, NOW - timedelta(hours=3), NOW, label="baseline", config={"note": "test"}
    )
    assert run["status"] == "done"
    assert run["label"] == "baseline"

    summary = run["summary"]
    assert summary["steps"] == 3
    assert summary["actions"] == {"open": 1, "hold": 1, "close": 1}
    # Rising closes: open early, close later → the crude mark-to-bars PnL is positive.
    assert summary["realized_pnl"] > 0
    assert summary["unrealized_pnl"] == 0  # flat after the close

    steps = await db.fetch_all("SELECT * FROM eval_steps ORDER BY ts")
    assert len(steps) == 3
    assert steps[0]["decision"]["action"] == "open"
    assert steps[0]["prompt"] and steps[0]["raw_output"]  # replay-ready, like the journal

    # The eval lane NEVER touches the live journal or the ledger.
    assert await db.fetch_all("SELECT * FROM decisions") == []
    assert await db.fetch_all("SELECT * FROM orders") == []


async def test_run_refuses_windows_beyond_max_steps(db: Database):
    harness = ReplayHarness(db, StubLLM([]), StubMarketData(), max_steps=10)
    try:
        await harness.run(BTC, NOW - timedelta(hours=50), NOW, label="too-wide")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert await db.fetch_all("SELECT * FROM eval_runs") == []  # refused before any row


async def test_closed_venue_steps_are_skipped_without_llm_calls(db: Database):
    # SPY over a Saturday: every step lands outside RTH. An LLM call would blow up the
    # empty StubLLM, so a clean 'done' run proves the model was never invoked.
    harness = ReplayHarness(db, StubLLM([]), StubMarketData())
    run = await harness.run(SPY, SATURDAY, SATURDAY + timedelta(hours=3), label="weekend")
    assert run["status"] == "done"
    assert run["summary"]["steps"] == 0
    assert run["summary"]["skipped_closed"] == 3
    assert await db.fetch_all("SELECT * FROM eval_steps") == []


async def test_decider_errors_are_recorded_and_do_not_kill_the_run(db: Database):
    await seed_hourly_bars(db)
    # Two malformed replies burn the decider's one retry → an error step; then a hold.
    harness = ReplayHarness(db, StubLLM(["not json", "still not json", HOLD]), StubMarketData())
    run = await harness.run(BTC, NOW - timedelta(hours=2), NOW, label="flaky")
    assert run["status"] == "done"
    assert run["summary"]["actions"] == {"hold": 1}
    assert run["summary"]["errors"] == 1


# --- surfaces ----------------------------------------------------------------


async def test_registry_runs_eval_in_background_and_lists_it(db: Database):
    from trader.surfaces.services import build_registry

    await seed_hourly_bars(db)
    harness = ReplayHarness(db, StubLLM([HOLD, HOLD]), StubMarketData())
    reg = build_registry(db, evaluator=harness)

    started = await reg.get("run_replay_eval").handler(
        symbol="BTC/USD",
        start=(NOW - timedelta(hours=2)).isoformat(),
        end=NOW.isoformat(),
        label="via-registry",
    )
    run_id = started["run_id"]
    assert started["status"] == "running"

    for _ in range(50):  # the run completes in the background
        row = await reg.get("eval_run").handler(run_id=run_id)
        if row["status"] == "done":
            break
        await asyncio.sleep(0.05)
    assert row["status"] == "done"
    assert row["summary"]["steps"] == 2
    assert len(row["steps"]) == 2
    assert "prompt" not in row["steps"][0]  # step listing is a summary, not a dump

    listed = await reg.get("eval_runs").handler()
    assert [r["label"] for r in listed] == ["via-registry"]


def test_eval_runs_surfaced_on_api_and_cli(clean_db: None, monkeypatch):
    import pytest  # noqa: F401
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app

    settings = Settings(auth_disabled=True)
    with TestClient(create_app(settings)) as tc:
        assert tc.get("/eval/runs").json() == []

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(cli.app, ["eval", "list"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
