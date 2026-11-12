"""Replay eval harness — rebuild historical context from the bars cache, rerun the
decider offline, journal to eval_runs/eval_steps (NEVER decisions), score crudely
(see docs/lessons.md#replay-before-flip).

The LLM is stubbed; the store is real. This harness is the gate-keeper for every future
decide-behavior change (docs/runbooks/decide-change-gate.md).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_loop import StubLLM, StubMarketData
from trader.core.symbols import AssetClass, default_registry, make_symbol
from trader.data.bars_cache import BarsCache
from trader.eval.replay import ReplayHarness, ReplayMarketData
from trader.loop.context import ContextBuilder
from trader.loop.decider import PROMPT_VARIANTS
from trader.loop.scheduler import Trigger
from trader.store.db import Database
from trader.store.repos import BarsRepo, BriefsRepo, MacroViewsRepo

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


async def test_run_replays_a_bots_prompt_variant_and_sleeve(db: Database):
    """A Bot version's decide rules and Sleeve can be replayed (#127): the system prompt
    is that variant's, and the context sizes against the Sleeve, as the live loop does."""
    await seed_hourly_bars(db)
    llm = StubLLM([HOLD, HOLD])
    harness = ReplayHarness(db, llm, StubMarketData())

    run = await harness.run(
        BTC,
        NOW - timedelta(hours=2),
        NOW,
        label="bot-2",
        config={"prompt_variant": "equity-largecap", "sleeve_usd": 5000},
    )
    assert run["status"] == "done"
    assert run["summary"]["prompt_variant"] == "equity-largecap"
    for messages in llm.calls:
        assert messages[0]["content"] == PROMPT_VARIANTS["equity-largecap"]
        assert "Sleeve equity: 5,000.00 USD" in messages[1]["content"]


async def test_run_defaults_to_bot_ones_prompt_and_the_account_equity(db: Database):
    await seed_hourly_bars(db)
    llm = StubLLM([HOLD])
    harness = ReplayHarness(db, llm, StubMarketData())

    run = await harness.run(BTC, NOW - timedelta(hours=1), NOW, label="baseline")
    assert run["summary"]["prompt_variant"] == "default"
    assert llm.calls[0][0]["content"] == PROMPT_VARIANTS["default"]
    assert "Account equity: 100,000.00 USD" in llm.calls[0][1]["content"]


async def test_run_refuses_an_unknown_prompt_variant_or_a_bad_sleeve(db: Database):
    harness = ReplayHarness(db, StubLLM([]), StubMarketData())
    for bad in ({"prompt_variant": "nope"}, {"sleeve_usd": 0}, {"sleeve_usd": "x"}):
        with pytest.raises(ValueError):
            await harness.create_run(BTC, NOW - timedelta(hours=1), NOW, label="bad", config=bad)
    assert await db.fetch_all("SELECT * FROM eval_runs") == []


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


async def _stamp_research(
    db: Database, table: str, version: int, column: str, ts: datetime
) -> None:
    await db.execute(
        f"UPDATE {table} SET {column} = %s WHERE version = %s",
        (ts, version),
    )


async def test_replay_uses_the_research_lane_current_at_each_step(db: Database):
    """A step sees the Macro View and brief that existed then, never a later compile.

    ``current()`` is today's row. Replay evidence is false if every historical step
    is scored against that row (or against no research lane at all).
    """
    await seed_hourly_bars(db)
    start = NOW - timedelta(hours=3)
    views = MacroViewsRepo(db)
    early_view = await views.add(
        compiled_by="stub:stub-model",
        global_thesis="Early thesis [note:1]",
        sections={"crypto": "Early crypto section [note:1]"},
        note_ids=[1],
    )
    later_view = await views.add(
        compiled_by="stub:stub-model",
        global_thesis="Later thesis [note:2]",
        sections={"crypto": "Later crypto section [note:2]"},
        note_ids=[2],
    )
    briefs = BriefsRepo(db)
    early_brief = await briefs.add(
        symbol="BTC/USD",
        compiled_by="stub:stub-model",
        thesis="Early brief [note:1]",
        points=["Early point [note:1]"],
        note_ids=[1],
    )
    later_brief = await briefs.add(
        symbol="BTC/USD",
        compiled_by="stub:stub-model",
        thesis="Later brief [note:2]",
        points=["Later point [note:2]"],
        note_ids=[2],
    )
    other = await briefs.add(
        symbol="SPY",
        compiled_by="stub:stub-model",
        thesis="Equity brief [note:3]",
        points=[],
        note_ids=[3],
    )
    # v1/b1 are current at the first step only; v2/b2 compile exactly at the second step.
    await _stamp_research(
        db, "macro_views", early_view, "created_at", start - timedelta(minutes=30)
    )
    await _stamp_research(db, "macro_views", later_view, "created_at", start + timedelta(hours=1))
    await _stamp_research(db, "briefs", early_brief, "ts", start - timedelta(minutes=30))
    await _stamp_research(db, "briefs", later_brief, "ts", start + timedelta(hours=1))
    await _stamp_research(db, "briefs", other, "ts", start - timedelta(minutes=30))

    harness = ReplayHarness(db, StubLLM([HOLD, HOLD, HOLD]), StubMarketData())
    run = await harness.run(BTC, start, NOW, label="research-asof")
    assert run["status"] == "done"
    steps = await db.fetch_all("SELECT ts, prompt, decision FROM eval_steps ORDER BY ts")
    assert len(steps) == 3

    first, second, third = steps
    assert "Early thesis" in first["prompt"] and "Early crypto section" in first["prompt"]
    assert "Later thesis" not in first["prompt"]
    assert "Early brief" in first["prompt"] and "Early point" in first["prompt"]
    assert "Later brief" not in first["prompt"]
    assert "Equity brief" not in first["prompt"]  # another symbol's brief never leaks
    assert first["decision"]["macro_view_version"] == early_view
    assert first["decision"]["brief_version"] == early_brief

    for step in (second, third):
        assert "Later thesis" in step["prompt"] and "Early thesis" not in step["prompt"]
        assert "Later brief" in step["prompt"] and "Early brief" not in step["prompt"]
        assert "Equity brief" not in step["prompt"]
        assert step["decision"]["macro_view_version"] == later_view
        assert step["decision"]["brief_version"] == later_brief


async def test_replay_can_ablate_briefs_without_dropping_the_macro_view(db: Database):
    """Briefs are dark on the live path. A baseline replay can omit them; Macro View stays."""
    await seed_hourly_bars(db)
    start = NOW - timedelta(hours=1)
    version = await MacroViewsRepo(db).add(
        compiled_by="stub:stub-model",
        global_thesis="Still the macro lane [note:1]",
        sections={"crypto": "Crypto still here [note:1]"},
        note_ids=[1],
    )
    await _stamp_research(db, "macro_views", version, "created_at", start - timedelta(minutes=5))
    await BriefsRepo(db).add(
        symbol="BTC/USD",
        compiled_by="stub:stub-model",
        thesis="Should stay out [note:1]",
        points=[],
        note_ids=[1],
    )

    harness = ReplayHarness(db, StubLLM([HOLD]), StubMarketData())
    run = await harness.run(BTC, start, NOW, label="briefs-off", config={"briefs": False})
    assert run["status"] == "done"
    step = await db.fetch_one("SELECT prompt, decision FROM eval_steps")
    assert step is not None
    assert "Still the macro lane" in step["prompt"]
    assert step["decision"]["macro_view_version"] == version
    assert "Candidate Brief" not in step["prompt"]
    assert "Should stay out" not in step["prompt"]
    assert "brief_version" not in step["decision"]


async def test_replay_can_ablate_the_macro_view_without_dropping_briefs(db: Database):
    """The symmetrical switch: a run can score the brief lane alone."""
    await seed_hourly_bars(db)
    start = NOW - timedelta(hours=1)
    version = await MacroViewsRepo(db).add(
        compiled_by="stub:stub-model",
        global_thesis="Macro should stay out [note:1]",
        sections={"crypto": "Crypto section out too [note:1]"},
        note_ids=[1],
    )
    await _stamp_research(db, "macro_views", version, "created_at", start - timedelta(minutes=5))
    brief = await BriefsRepo(db).add(
        symbol="BTC/USD",
        compiled_by="stub:stub-model",
        thesis="Brief still here [note:1]",
        points=[],
        note_ids=[1],
    )
    await _stamp_research(db, "briefs", brief, "ts", start - timedelta(minutes=5))

    harness = ReplayHarness(db, StubLLM([HOLD]), StubMarketData())
    run = await harness.run(BTC, start, NOW, label="macro-off", config={"macro_view": False})
    assert run["status"] == "done"
    step = await db.fetch_one("SELECT prompt, decision FROM eval_steps")
    assert step is not None
    assert "Brief still here" in step["prompt"]
    assert step["decision"]["brief_version"] == brief
    assert "Operator Macro View" not in step["prompt"]
    assert "Macro should stay out" not in step["prompt"]
    assert "macro_view_version" not in step["decision"]


# --- surfaces ----------------------------------------------------------------


class RecordingEvaluator:
    """Stands in for the harness on the API/CLI doors: records each run's config."""

    def __init__(self) -> None:
        self.configs: list[dict] = []
        self.executed: list[dict] = []  # each execute's keyword arguments

    async def create_run(self, symbol, start, end, *, label, config) -> int:
        self.configs.append(config)
        return len(self.configs)

    async def execute(self, run_id, symbol, **kwargs) -> dict:
        self.executed.append(kwargs)
        return {"id": run_id, "status": "done"}


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


async def test_registry_forwards_the_research_lane_flags_into_the_run_config(db: Database):
    from trader.surfaces.services import build_registry

    evaluator = RecordingEvaluator()
    reg = build_registry(db, evaluator=evaluator)
    window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}

    await reg.get("run_replay_eval").handler(symbol="BTC/USD", **window)
    await reg.get("run_replay_eval").handler(
        symbol="BTC/USD", briefs=False, macro_view=False, **window
    )
    assert evaluator.configs == [
        {"desk": False, "briefs": True, "macro_view": True},
        {"desk": False, "briefs": False, "macro_view": False},
    ]
    audit = await db.fetch_all("SELECT payload FROM audit_events ORDER BY id")
    assert audit[-1]["payload"]["briefs"] is False
    assert audit[-1]["payload"]["macro_view"] is False
    # No model: the harness decides on its own registry, exactly as before.
    assert evaluator.executed == [{}, {}]


async def _wait_done(reg, run_id: int) -> dict:
    for _ in range(100):
        row = await reg.get("eval_run").handler(run_id=run_id)
        if row["status"] != "running":
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never finished")


async def test_a_run_replays_decide_on_a_named_provider_and_model(db: Database):
    from tests.test_profiles import DECIDE_OK, EnvClient, ModelServer, with_local
    from trader.llm.registry import LLMRegistry
    from trader.llm.usage import UsageMeter
    from trader.store.repos import UsageRepo
    from trader.surfaces.services import build_registry

    await seed_hourly_bars(db)
    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK})
    _, profiles = await with_local(db, server)
    env = EnvClient("env-decide", HOLD)
    llm = LLMRegistry({"decide": [env]}, UsageMeter(UsageRepo(db)), profiles=profiles)
    reg = build_registry(db, evaluator=ReplayHarness(db, llm, StubMarketData()), profiles=profiles)
    window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}

    started = await reg.get("run_replay_eval").handler(
        symbol="BTC/USD", label="q8", model="spark:qwen3:8b", **window
    )
    row = await _wait_done(reg, started["run_id"])

    assert row["status"] == "done", row
    assert row["summary"]["steps"] == 2
    assert row["config"]["model"] == "spark:qwen3:8b"
    assert server.models("spark") == ["qwen3:8b", "qwen3:8b"]  # every step, no Probe
    assert env.calls == 0
    # Metered as every replay is: into usage_daily under decide, for the model it used.
    usage = await db.fetch_all("SELECT role, provider, model, requests FROM usage_daily")
    assert [(u["role"], u["provider"], u["model"], u["requests"]) for u in usage] == [
        ("decide", "spark", "qwen3:8b", 2)
    ]
    audit = await db.fetch_one(
        "SELECT payload FROM audit_events WHERE action = 'run_replay_eval' ORDER BY id DESC"
    )
    assert audit["payload"]["model"] == "spark:qwen3:8b"

    # Unset, the same harness decides on its own registry again.
    plain = await reg.get("run_replay_eval").handler(symbol="BTC/USD", **window)
    row = await _wait_done(reg, plain["run_id"])
    assert row["status"] == "done" and "model" not in row["config"]
    assert env.calls == 2
    assert server.models("spark") == ["qwen3:8b", "qwen3:8b"]


async def test_a_bad_model_spec_is_refused_before_any_run_exists(db: Database):
    from tests.test_profiles import ModelServer, with_local
    from trader.surfaces.services import build_registry

    server = ModelServer({})
    providers, profiles = await with_local(db, server)
    await providers.add(name="kev", preset="jev", api_key="kev-key-12345")
    window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}
    evaluator = RecordingEvaluator()
    reg = build_registry(db, evaluator=evaluator, profiles=profiles)

    refusals = {
        "qwen3": "not provider:model",
        "ghost:m": "no Provider named 'ghost'",
        "kev:m": "answers question sets only",
    }
    for spec, reason in refusals.items():
        with pytest.raises(ValueError, match=reason):
            await reg.get("run_replay_eval").handler(symbol="BTC/USD", model=spec, **window)
    # A build without the Provider store cannot honour a model either.
    bare = build_registry(db, evaluator=evaluator)
    with pytest.raises(ValueError, match="no Provider store"):
        await bare.get("run_replay_eval").handler(symbol="BTC/USD", model="spark:m", **window)

    assert evaluator.configs == []
    assert server.calls == []
    audit = await db.fetch_all("SELECT id FROM audit_events WHERE action = 'run_replay_eval'")
    assert audit == []


def test_eval_run_model_on_api_and_cli(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.api.app as app_mod
    import trader.surfaces.cli.main as cli
    from trader.llm.profiles import ProfileDesk, ProfileError
    from trader.ops.config import Settings

    pinned = object()

    async def client_for(self, role, provider, model):
        if provider != "grok-deep":
            raise ProfileError(f"no Provider named {provider!r}")
        assert (role, model) == ("decide", "grok-4.7")
        return pinned

    monkeypatch.setattr(ProfileDesk, "client_for", client_for)
    evaluator = RecordingEvaluator()
    monkeypatch.setattr(app_mod, "build_evaluator", lambda db, settings, desk=None, **kw: evaluator)
    settings = Settings(auth_disabled=True)
    window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}

    with TestClient(app_mod.create_app(settings)) as tc:
        ok = tc.post(
            "/eval/replay", params={"symbol": "BTC/USD", "model": "grok-deep:grok-4.7", **window}
        )
        bad = tc.post("/eval/replay", params={"symbol": "BTC/USD", "model": "ghost:m", **window})
    assert ok.status_code == 200, ok.text
    assert bad.status_code == 422 and "ghost" in bad.json()["detail"]
    assert evaluator.configs == [
        {"desk": False, "briefs": True, "macro_view": True, "model": "grok-deep:grok-4.7"}
    ]
    assert evaluator.executed == [{"decide": pinned}]

    evaluator.configs.clear()
    monkeypatch.setattr(cli, "_client", lambda: TestClient(app_mod.create_app(settings)))
    base = ["eval", "run", "--symbol", "BTC/USD", "--start", window["start"]]
    base += ["--end", window["end"]]
    result = CliRunner().invoke(cli.app, [*base, "--model", "grok-deep:grok-4.7"])
    assert result.exit_code == 0, result.output
    assert evaluator.configs[-1]["model"] == "grok-deep:grok-4.7"


def test_eval_run_lane_flags_on_api_and_cli(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.api.app as app_mod
    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings

    evaluator = RecordingEvaluator()
    monkeypatch.setattr(app_mod, "build_evaluator", lambda db, settings, desk=None, **kw: evaluator)
    settings = Settings(auth_disabled=True)
    window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}

    with TestClient(app_mod.create_app(settings)) as tc:
        assert tc.post("/eval/replay", params={"symbol": "BTC/USD", **window}).status_code == 200
        resp = tc.post(
            "/eval/replay",
            params={"symbol": "BTC/USD", "briefs": False, "macro_view": False, **window},
        )
        assert resp.status_code == 200
    assert evaluator.configs == [
        {"desk": False, "briefs": True, "macro_view": True},
        {"desk": False, "briefs": False, "macro_view": False},
    ]

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
    result = CliRunner().invoke(cli.app, [*base, "--no-briefs", "--no-macro-view"])
    assert result.exit_code == 0, result.output
    assert evaluator.configs == [
        {"desk": False, "briefs": True, "macro_view": True},
        {"desk": False, "briefs": False, "macro_view": False},
    ]


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


# --- the deterministic stop in replay -------------------------------------------------

# Seeded long at 70,000 against closes near 60,500: about -13.6%, past any 8% stop.
UNDERWATER = {"qty": 0.01, "avg_entry_price": 70_000.0}


async def test_replay_runs_the_stop_before_the_model_and_honors_its_cooldown(db: Database):
    """With the live limits wired in, a replayed position past its stop is closed by the
    stop at that step (no model call, as live), and the cooldown keeps the model's next
    opens on the symbol out of the crude book."""
    import pytest

    from trader.risk.limits import RiskLimits

    await seed_hourly_bars(db)
    llm = StubLLM([OPEN, OPEN])
    harness = ReplayHarness(
        db, llm, StubMarketData(), limits=RiskLimits(stop_loss_frac=0.08, stop_vol_mult=2.0)
    )
    run = await harness.run(
        BTC,
        NOW - timedelta(hours=3),
        NOW,
        label="stop",
        config={"exit_review": True, "exit_review_seed": UNDERWATER},
    )

    summary = run["summary"]
    assert len(llm.calls) == 2  # the stop step never asked the model
    assert summary["stops"] == 1 and summary["stop_cooldown_denied"] == 2
    steps = await db.fetch_all("SELECT ts, price, decision FROM eval_steps ORDER BY ts")
    first = steps[0]["decision"]
    assert first["action"] == "close" and first["trigger"] == "stop_loss"
    assert first["rationale"].startswith("stop-loss: -13.")
    assert summary["realized_pnl"] == pytest.approx((float(steps[0]["price"]) - 70_000.0) * 0.01)
    assert summary["unrealized_pnl"] == 0  # the denied opens never entered the book


def test_the_api_replays_with_the_live_stop(monkeypatch):
    from trader.ops.config import Settings
    from trader.risk.limits import limits_from_settings
    from trader.surfaces.api.app import build_evaluator

    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    settings = Settings()
    harness = build_evaluator(None, settings, llm=object())  # type: ignore[arg-type]
    assert harness._limits == limits_from_settings(settings)


async def test_replay_without_limits_has_no_stop(db: Database):
    await seed_hourly_bars(db)
    llm = StubLLM([HOLD, HOLD, HOLD])
    harness = ReplayHarness(db, llm, StubMarketData())
    run = await harness.run(
        BTC,
        NOW - timedelta(hours=3),
        NOW,
        label="no-stop",
        config={"exit_review": True, "exit_review_seed": UNDERWATER},
    )
    assert len(llm.calls) == 3
    assert run["summary"]["stops"] == 0 and run["summary"]["realized_pnl"] == 0


# --- the confirm stage in replay ------------------------------------------------------


class JevReplayLLM:
    """LLMRegistry-shaped: scripted decide replies, and a Jev that answers every asked
    judgment with ``answers[qid]`` (default 0.9) and records each state it was sent."""

    def __init__(
        self,
        decide: list[str],
        answers: dict[str, float] | None = None,
        costs: dict[str, float] | None = None,
    ) -> None:
        self._decide = list(decide)
        self.answers = answers or {}
        self.costs = costs or {}
        self.states: list[dict] = []

    async def complete(
        self, role, messages, *, schema=None, max_tokens=1024, temperature=0.2, state=None
    ):
        from trader.llm.base import Completion, Usage

        if role == "confirm":
            self.states.append(state)
            text = json.dumps(
                {q: {"type": "noul", "noul": self.answers.get(q, 0.9)} for q in schema}
            )
        else:
            text = self._decide.pop(0)
        return Completion(
            text=text,
            usage=Usage(prompt_tokens=100, completion_tokens=20),
            cost_usd=self.costs.get(role, 0.0),
            provider="stub",
            model="jev-1.15" if role == "confirm" else "stub-model",
        )


UNGROUNDED = {"grounded": 0.1}  # an open the rule vetoes


async def _confirm_steps(db: Database, run_id: int) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT decision FROM eval_steps WHERE run_id = %s ORDER BY ts", (run_id,)
    )
    return [r["decision"] for r in rows]


async def test_a_veto_on_replay_turns_the_open_jev_vetoes_into_a_hold(db: Database):
    await seed_hourly_bars(db)
    llm = JevReplayLLM([OPEN, HOLD], UNGROUNDED)
    run = await ReplayHarness(db, llm, StubMarketData()).run(
        BTC, NOW - timedelta(hours=2), NOW, label="veto-on", config={"confirm_veto": True}
    )
    assert run["status"] == "done", run["summary"]
    assert run["config"]["confirm_veto"] is True
    # The book stays flat: the vetoed open never fills.
    assert run["summary"]["actions"] == {"hold": 2}
    assert run["summary"]["realized_pnl"] == 0 and run["summary"]["unrealized_pnl"] == 0
    first, second = await _confirm_steps(db, run["id"])
    assert first["action"] == "hold"
    assert first["conviction"] == 0.8
    assert first["rationale"].startswith("confirm veto: ")
    assert "intent" not in first and "fill_cost" not in first
    assert first["confirm"]["vetoed"] is True
    assert first["confirm"]["verdict"] == "veto"
    assert first["confirm"]["draft"] == {"action": "open", "side": "buy"}
    assert "confirm" not in second  # a hold Draft is never sent to Jev
    assert len(llm.states) == 1


async def test_a_veto_off_replay_keeps_the_open_and_records_would_veto(db: Database):
    await seed_hourly_bars(db)
    llm = JevReplayLLM([OPEN, HOLD], UNGROUNDED)
    run = await ReplayHarness(db, llm, StubMarketData()).run(
        BTC, NOW - timedelta(hours=2), NOW, label="veto-off"
    )
    assert run["status"] == "done", run["summary"]
    assert run["config"]["confirm_veto"] is False  # always written, so pairs compare
    assert run["summary"]["actions"] == {"open": 1, "hold": 1}
    assert run["summary"]["unrealized_pnl"] != 0  # the open filled
    first, _ = await _confirm_steps(db, run["id"])
    assert first["action"] == "open" and first["rationale"] == "momentum"
    assert first["confirm"]["would_veto"] is True
    assert first["confirm"]["vetoed"] is False
    assert first["confirm"]["verdict"] == "pass"


async def test_the_run_cost_includes_the_jev_calls(db: Database):
    await seed_hourly_bars(db)
    llm = JevReplayLLM([OPEN, HOLD], costs={"decide": 0.01, "confirm": 0.002})
    run = await ReplayHarness(db, llm, StubMarketData()).run(
        BTC, NOW - timedelta(hours=2), NOW, label="cost"
    )
    assert run["status"] == "done", run["summary"]
    assert len(llm.states) == 1  # one open judged; the hold is never sent
    first, _ = await _confirm_steps(db, run["id"])
    assert first["confirm"]["cost_usd"] == 0.002
    assert run["summary"]["cost_usd"] == pytest.approx(0.022)


async def test_a_replay_without_a_confirm_route_records_no_confirm(db: Database):
    await seed_hourly_bars(db)
    run = await ReplayHarness(db, StubLLM([OPEN, CLOSE]), StubMarketData()).run(
        BTC, NOW - timedelta(hours=2), NOW, label="no-jev", config={"confirm_veto": True}
    )
    assert run["status"] == "done", run["summary"]
    assert run["summary"]["actions"] == {"open": 1, "close": 1}
    assert all("confirm" not in d for d in await _confirm_steps(db, run["id"]))


async def test_replay_tells_jev_the_position_its_own_book_opened(db: Database):
    """A close is judged against the walk's in-memory book: the side and size it holds,
    and the rationale and time of the step that opened it (never the live journal)."""
    await seed_hourly_bars(db)
    llm = JevReplayLLM([OPEN, CLOSE])
    start = NOW - timedelta(hours=2)
    run = await ReplayHarness(db, llm, StubMarketData()).run(
        BTC, start, NOW, label="held", config={"confirm_veto": True}
    )
    assert run["status"] == "done", run["summary"]
    assert run["summary"]["actions"] == {"open": 1, "close": 1}
    opened, closed = llm.states
    assert "position" not in opened
    held = closed["position"]
    assert held["side"] == "long"
    first = await db.fetch_one(
        "SELECT price FROM eval_steps WHERE run_id = %s ORDER BY ts LIMIT 1", (run["id"],)
    )
    assert held["quantity"] == pytest.approx(1000 / float(first["price"]))  # the book's qty
    assert held["opened_at"] == start.isoformat()
    assert held["open_rationale"] == "momentum"
    _, close_step = await _confirm_steps(db, run["id"])
    assert close_step["confirm"]["verdict"] == "recorded"
    assert "thesis_broken" in close_step["confirm"]["answers"]


def test_eval_run_confirm_veto_on_api_and_cli(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.api.app as app_mod
    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings

    evaluator = RecordingEvaluator()
    monkeypatch.setattr(app_mod, "build_evaluator", lambda db, settings, desk=None, **kw: evaluator)
    settings = Settings(auth_disabled=True)
    window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}
    with TestClient(app_mod.create_app(settings)) as tc:
        resp = tc.post("/eval/replay", params={"symbol": "BTC/USD", "confirm_veto": True, **window})
        assert resp.status_code == 200, resp.text
    assert evaluator.configs[-1]["confirm_veto"] is True

    monkeypatch.setattr(cli, "_client", lambda: TestClient(app_mod.create_app(settings)))
    base = ["eval", "run", "--symbol", "BTC/USD", "--start", window["start"]]
    base += ["--end", window["end"]]
    result = CliRunner().invoke(cli.app, [*base, "--confirm-veto"])
    assert result.exit_code == 0, result.output
    assert evaluator.configs[-1]["confirm_veto"] is True
    result = CliRunner().invoke(cli.app, [*base, "--no-confirm-veto"])
    assert result.exit_code == 0, result.output
    assert "confirm_veto" not in evaluator.configs[-1]  # off: the harness writes False


async def test_the_mcp_tool_takes_confirm_veto(db: Database):
    from mcp.client import Client

    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    evaluator = RecordingEvaluator()
    async with Client(build_mcp(build_registry(db, evaluator=evaluator))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "confirm_veto" in tools["run_replay_eval"].input_schema["properties"]
        window = {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}
        await client.call_tool(
            "run_replay_eval", {"symbol": "BTC/USD", "confirm_veto": True, **window}
        )
    assert evaluator.configs[-1]["confirm_veto"] is True
    audit = await db.fetch_all("SELECT payload FROM audit_events ORDER BY id")
    assert audit[-1]["payload"]["confirm_veto"] is True
