"""Exit-review lane (#34) — a held-position block in the decide prompt, dark by default,
replay-gated with a seeded position so the block has something to review.

The LLM and market data are stubbed; the store is real. Nothing here reaches RiskGuard:
the block is advisory by construction, and a test asserts that structurally.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.test_loop import BTC, HOLD, NOW, StubLLM, StubMarketData, build_runner
from tests.test_replay_eval import CLOSE, OPEN, RecordingEvaluator, seed_hourly_bars
from trader.eval.replay import ReplayHarness
from trader.loop.context import (
    HELD_POSITION_BUDGET_CHARS,
    ContextBuilder,
    build_held_position_slice,
    unrealized_pnl_frac,
)
from trader.loop.decider import SYSTEM_PROMPT, Decider
from trader.loop.journal import Journal
from trader.store.db import Database
from trader.store.repos import PositionsRepo, SentimentRepo

HELD = {"qty": 1.0, "avg_entry_price": 150.0, "updated_at": NOW - timedelta(days=3, hours=4)}
BLOCK = "Held position review"


class StubSentiment:
    """SentimentRepo-shaped: ``latest`` for live, ``recent`` for the as-of replay path."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def latest(self, symbol: str, *, status: str = "ok") -> dict | None:
        rows = [r for r in self._rows if r["symbol"] == symbol and r["status"] == status]
        return max(rows, key=lambda r: r["ts"]) if rows else None

    async def recent(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        rows = [r for r in self._rows if symbol is None or r["symbol"] == symbol]
        return sorted(rows, key=lambda r: r["ts"], reverse=True)[:limit]


def sentiment_row(ts: datetime, score: float, symbol: str = "BTC/USD") -> dict:
    return {"symbol": symbol, "status": "ok", "score": score, "volume": "elevated", "ts": ts}


# --- settings ----------------------------------------------------------------


def test_settings_exit_review_defaults_dark():
    from trader.ops.config import Settings

    assert Settings(auth_disabled=True).exit_review_enabled is False


# --- the block ---------------------------------------------------------------


def test_unrealized_pnl_frac_is_signed_against_the_position():
    assert unrealized_pnl_frac(110.0, 1.0, 100.0) == pytest.approx(0.10)
    assert unrealized_pnl_frac(110.0, -1.0, 100.0) == pytest.approx(-0.10)
    assert unrealized_pnl_frac(110.0, 0.0, 100.0) is None
    assert unrealized_pnl_frac(110.0, 1.0, None) is None
    assert unrealized_pnl_frac(0.0, 1.0, 100.0) is None


async def test_block_present_only_with_flag_and_a_held_position():
    dark = ContextBuilder(StubMarketData())
    lit = ContextBuilder(StubMarketData(), exit_review_enabled=True)
    kwargs = dict(now=NOW, triggers=[], equity=100_000.0)

    off = await dark.build(BTC, position_qty=1.0, position=HELD, **kwargs)
    assert off.held_position is None
    assert BLOCK not in off.to_prompt()

    flat = await lit.build(BTC, position_qty=0.0, position=None, **kwargs)
    assert flat.held_position is None
    assert BLOCK not in flat.to_prompt()

    held = await lit.build(BTC, position_qty=1.0, position=HELD, **kwargs)
    assert held.held_position is not None
    prompt = held.to_prompt()
    assert BLOCK in prompt
    assert "Entry price: 150" in prompt
    assert "long 1 units" in prompt
    assert "3d 4h" in prompt  # age derived from the row's updated_at
    assert "Unrealized P&L: +6.00%" in prompt  # 159 vs 150
    assert "original thesis still holds" in prompt
    assert "`close`" in prompt and "`hold`" in prompt


async def test_per_call_switch_overrides_the_builder_default():
    """Replay flips the lane per run without rebuilding the harness."""
    dark = ContextBuilder(StubMarketData())
    kwargs = dict(now=NOW, triggers=[], equity=100_000.0, position_qty=1.0, position=HELD)
    assert (await dark.build(BTC, exit_review=True, **kwargs)).held_position is not None
    lit = ContextBuilder(StubMarketData(), exit_review_enabled=True)
    assert (await lit.build(BTC, exit_review=False, **kwargs)).held_position is None


async def test_block_carries_the_latest_sentiment_pinned_to_the_step():
    rows = [
        sentiment_row(NOW - timedelta(days=2), -0.6),
        sentiment_row(NOW - timedelta(hours=1), 0.4),
        sentiment_row(NOW - timedelta(hours=1), 0.9, symbol="ETH/USD"),  # another symbol
    ]
    builder = ContextBuilder(
        StubMarketData(), exit_review_enabled=True, sentiment=StubSentiment(rows)
    )
    kwargs = dict(triggers=[], equity=100_000.0, position_qty=1.0, position=HELD)

    live = await builder.build(BTC, now=NOW, **kwargs)
    assert "Latest X sentiment: +0.40" in live.to_prompt()

    # A replayed step sees the sentiment that existed then, never a later poll.
    then = NOW - timedelta(days=1)
    pinned = await builder.build(BTC, now=then, research_as_of=then, **kwargs)
    assert "Latest X sentiment: -0.60" in pinned.to_prompt()
    assert "+0.40" not in pinned.to_prompt()

    # No sentiment row → the line is simply absent; the block still asks its question.
    none = await ContextBuilder(StubMarketData(), exit_review_enabled=True).build(
        BTC, now=NOW, **kwargs
    )
    assert "Latest X sentiment" not in none.to_prompt()
    assert BLOCK in none.to_prompt()


def test_block_entries_drop_whole_under_the_budget():
    full = build_held_position_slice(HELD, price=159.0, now=NOW, sentiment=None)
    assert full is not None
    assert sum(len(e) for e in full.entries) <= HELD_POSITION_BUDGET_CHARS
    assert len(full.entries) >= 4  # entry, age, pnl, the ask

    # Under a tight budget whole entries fall away; none is truncated.
    tight = build_held_position_slice(
        HELD, price=159.0, now=NOW, sentiment=None, budget_chars=len(full.entries[0]) + 5
    )
    assert tight is not None
    assert len(tight.entries) < len(full.entries)
    assert all(entry in full.entries for entry in tight.entries)
    assert sum(len(e) for e in tight.entries) <= len(full.entries[0]) + 5

    # A budget that fits nothing means no block at all.
    assert (
        build_held_position_slice(HELD, price=159.0, now=NOW, sentiment=None, budget_chars=3)
        is None
    )
    # No entry price → nothing to review against.
    assert build_held_position_slice({"qty": 1.0}, price=159.0, now=NOW, sentiment=None) is None


# --- firewall ----------------------------------------------------------------


def test_riskguard_is_structurally_firewalled_from_the_exit_review():
    """RiskGuard never reads the held-position block: no import, identifier or field."""
    import inspect
    from dataclasses import fields

    from trader.risk.guard import RiskContext, RiskGuard

    surface = [p.lower() for p in inspect.signature(RiskGuard.evaluate).parameters]
    surface += [f.name.lower() for f in fields(RiskContext)]
    for name in surface:
        assert "held_position" not in name and "exit_review" not in name

    forbidden = {"held_position", "HeldPositionSlice", "build_held_position_slice", "exit_review"}
    risk_dir = Path(__file__).resolve().parents[1] / "src" / "trader" / "risk"
    sources = list(risk_dir.glob("*.py"))
    assert sources
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "") != "trader.loop.context", path.name
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden, f"{path.name}: {node.id}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, f"{path.name}: {node.attr}"


# --- decider -----------------------------------------------------------------


async def test_system_prompt_describes_exits_only_when_reviewing():
    kwargs = dict(now=NOW, triggers=[], equity=100_000.0, position_qty=1.0, position=HELD)
    plain = await ContextBuilder(StubMarketData()).build(BTC, **kwargs)
    reviewing = await ContextBuilder(StubMarketData(), exit_review_enabled=True).build(
        BTC, **kwargs
    )

    llm = StubLLM([HOLD, HOLD])
    await Decider(llm).decide(plain)
    await Decider(llm).decide(reviewing)
    baseline_system, review_system = (calls[0]["content"] for calls in llm.calls)

    assert baseline_system == SYSTEM_PROMPT  # unchanged while the lane is dark
    assert review_system.startswith(SYSTEM_PROMPT)
    assert "`close`" in review_system and "reduce" in review_system
    assert "notional_usd" in review_system  # how a partial exit is expressed on the wire


# --- runner plumbing -----------------------------------------------------------


async def test_runner_keeps_the_lane_dark_by_default(db: Database):
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=1.0, avg_entry_price=150.0
    )
    results = await build_runner(db, [HOLD]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert "Open position: 1 units" in row["prompt"]
    assert BLOCK not in row["prompt"]


async def test_runner_plumbs_the_flag_and_the_position_row(db: Database):
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=1.0, avg_entry_price=150.0
    )
    await SentimentRepo(db).insert(symbol="BTC/USD", status="ok", score=-0.55, volume="storm")
    results = await build_runner(db, [HOLD], exit_review_enabled=True).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert BLOCK in row["prompt"]
    assert "Entry price: 150" in row["prompt"]
    assert "Latest X sentiment: -0.55" in row["prompt"]
    # Advisory: the decision is journaled as a plain shadow hold, no verdicts, no orders.
    assert row["risk_verdicts"] is None
    assert await db.fetch_all("SELECT * FROM orders") == []


# --- replay ------------------------------------------------------------------

SEED = {"qty": 0.2, "avg_entry_price": 100.0}


async def test_replay_seed_puts_the_block_in_every_step_prompt(db: Database):
    await seed_hourly_bars(db)
    start = NOW - timedelta(hours=3)
    harness = ReplayHarness(db, StubLLM([HOLD, HOLD, CLOSE]), StubMarketData())
    run = await harness.run(
        BTC,
        start,
        NOW,
        label="exit-review-on",
        config={"exit_review": True, "exit_review_seed": SEED},
    )
    assert run["status"] == "done"
    summary = run["summary"]
    assert summary["exit_review_seed"] == SEED
    assert summary["actions"] == {"hold": 2, "close": 1}
    assert summary["exit_review"] is True

    steps = await db.fetch_all("SELECT ts, price, prompt, decision FROM eval_steps ORDER BY ts")
    assert len(steps) == 3
    # The crude PnL starts from the seed: the close realizes against the seeded entry.
    assert summary["realized_pnl"] == pytest.approx((float(steps[2]["price"]) - 100.0) * 0.2)
    assert summary["unrealized_pnl"] == 0
    for step in steps:
        assert "Open position: 0.2 units" in step["prompt"]
        assert BLOCK in step["prompt"]
        assert "Entry price: 100" in step["prompt"]
        assert step["decision"]["held_position"]
    # Age counts from the window start (or the seed's opened_at) — the third step is 2h in.
    assert "2h" in steps[2]["prompt"]


async def test_replay_seed_without_the_flag_is_the_baseline(db: Database):
    """Baseline vs candidate share the seeded book; only the block differs."""
    await seed_hourly_bars(db)
    harness = ReplayHarness(db, StubLLM([HOLD]), StubMarketData())
    run = await harness.run(
        BTC,
        NOW - timedelta(hours=1),
        NOW,
        label="exit-review-off",
        config={"exit_review_seed": SEED},
    )
    assert run["status"] == "done"
    step = await db.fetch_one("SELECT price, prompt, decision FROM eval_steps")
    assert "Open position: 0.2 units" in step["prompt"]
    assert BLOCK not in step["prompt"]
    assert "held_position" not in step["decision"]
    assert run["summary"]["exit_review"] is False
    # Never closed: the seed's mark-to-bars move rides as unrealized.
    assert run["summary"]["unrealized_pnl"] == pytest.approx((float(step["price"]) - 100.0) * 0.2)


async def test_replay_default_follows_the_harness_setting(db: Database):
    await seed_hourly_bars(db)
    lit = ReplayHarness(db, StubLLM([HOLD]), StubMarketData(), exit_review_enabled=True)
    run = await lit.run(
        BTC, NOW - timedelta(hours=1), NOW, label="from-settings", config={"exit_review_seed": SEED}
    )
    assert run["status"] == "done"
    step = await db.fetch_one("SELECT prompt FROM eval_steps")
    assert BLOCK in step["prompt"]


async def test_replay_without_a_seed_starts_flat_and_reviews_after_an_open(db: Database):
    await seed_hourly_bars(db)
    harness = ReplayHarness(db, StubLLM([OPEN, HOLD]), StubMarketData())
    run = await harness.run(
        BTC, NOW - timedelta(hours=2), NOW, label="flat-start", config={"exit_review": True}
    )
    assert run["status"] == "done"
    first, second = await db.fetch_all("SELECT prompt FROM eval_steps ORDER BY ts")
    assert BLOCK not in first["prompt"]  # nothing held yet
    assert BLOCK in second["prompt"]  # the sim's own open is now under review


async def test_replay_refuses_a_malformed_seed(db: Database):
    harness = ReplayHarness(db, StubLLM([]), StubMarketData())
    for bad in (
        {"qty": 0.0, "avg_entry_price": 100.0},
        {"qty": 1.0},
        {"qty": 1.0, "avg_entry_price": -1},
    ):
        with pytest.raises(ValueError, match="exit_review_seed"):
            await harness.create_run(
                BTC, NOW - timedelta(hours=1), NOW, label="bad", config={"exit_review_seed": bad}
            )
    assert await db.fetch_all("SELECT * FROM eval_runs") == []


# --- surfaces ----------------------------------------------------------------


async def test_registry_forwards_the_exit_review_flags(db: Database):
    from trader.surfaces.services import build_registry

    evaluator = RecordingEvaluator()
    reg = build_registry(db, evaluator=evaluator)
    window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}

    await reg.get("run_replay_eval").handler(symbol="BTC/USD", **window)
    await reg.get("run_replay_eval").handler(
        symbol="BTC/USD", exit_review=True, exit_review_seed=SEED, **window
    )
    assert evaluator.configs == [
        {"desk": False, "briefs": True, "macro_view": True},  # absent → follows Settings
        {
            "desk": False,
            "briefs": True,
            "macro_view": True,
            "exit_review": True,
            "exit_review_seed": SEED,
        },
    ]
    audit = await db.fetch_all("SELECT payload FROM audit_events ORDER BY id")
    assert audit[-1]["payload"]["exit_review"] is True
    assert audit[-1]["payload"]["exit_review_seed"] == SEED


def test_exit_review_flags_on_api_and_cli(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.api.app as app_mod
    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings

    evaluator = RecordingEvaluator()
    monkeypatch.setattr(app_mod, "build_evaluator", lambda db, settings, desk=None, **kw: evaluator)
    settings = Settings(auth_disabled=True)
    window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}
    expected = [
        {"desk": False, "briefs": True, "macro_view": True},
        {"desk": False, "briefs": True, "macro_view": True, "exit_review": False},
        {
            "desk": False,
            "briefs": True,
            "macro_view": True,
            "exit_review": True,
            "exit_review_seed": {"qty": 0.2, "avg_entry_price": 2541.5},
        },
    ]

    with TestClient(app_mod.create_app(settings)) as tc:
        assert tc.post("/eval/replay", params={"symbol": "BTC/USD", **window}).status_code == 200
        resp = tc.post("/eval/replay", params={"symbol": "BTC/USD", "exit_review": False, **window})
        assert resp.status_code == 200
        resp = tc.post(
            "/eval/replay",
            params={
                "symbol": "BTC/USD",
                "exit_review": True,
                "seed_qty": 0.2,
                "seed_entry": 2541.5,
                **window,
            },
        )
        assert resp.status_code == 200
        # Half a seed is a mistake, not a flat start.
        resp = tc.post("/eval/replay", params={"symbol": "BTC/USD", "seed_qty": 0.2, **window})
        assert resp.status_code == 422
    assert evaluator.configs == expected

    evaluator.configs.clear()
    monkeypatch.setattr(cli, "_client", lambda: TestClient(app_mod.create_app(settings)))
    base = [
        "eval",
        "run",
        "--symbol",
        "BTC/USD",
        "--start",
        window["start"],
        "--end",
        window["end"],
    ]
    assert CliRunner().invoke(cli.app, base).exit_code == 0
    result = CliRunner().invoke(cli.app, [*base, "--no-exit-review"])
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(
        cli.app, [*base, "--exit-review", "--seed-qty", "0.2", "--seed-entry", "2541.5"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "running"
    assert evaluator.configs == expected
