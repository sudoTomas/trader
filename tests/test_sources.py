"""Sources (#124): what a Bot reads when it decides, each read stamped with a Source version.

Contract per Source: ``as_of=None`` reads current, a timestamp pins the read to the state
that existed then, and the same ``as_of`` gives the same version. Bot #1's prompts are
held byte-identical to the pre-Sources ContextBuilder (``tests/golden``). The journal and
replay record ``source_versions``; the risk package never imports Sources.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.source_fixtures import (
    BTC,
    HELD,
    PARITY_SCENARIOS,
    SPY,
    StubBriefs,
    StubFred,
    StubMacroViews,
    StubSentiment,
    parity_triggers,
)
from tests.test_loop import HOLD, NOW, StubLLM, StubMarketData, build_runner
from tests.test_replay_eval import seed_hourly_bars
from trader.eval.replay import ReplayHarness
from trader.loop.bots import BOT_ONE, BotDesk, bot_one_spec
from trader.loop.context import ContextBuilder
from trader.loop.journal import Journal
from trader.loop.sources import (
    BriefSource,
    FredSource,
    HeldPositionSource,
    MacroViewSource,
    SentimentSource,
    Subscription,
    bot_one_subscriptions,
)
from trader.ops.config import Settings
from trader.store.db import Database
from trader.store.repos import BriefsRepo, MacroViewsRepo, PositionsRepo, SentimentRepo

GOLDEN = Path(__file__).parent / "golden" / "bot_one_prompts.json"
LIVE = {"now": NOW}


def stub_sources(fred: StubFred | None = None) -> dict:
    sentiment = SentimentSource(StubSentiment())
    return {
        "macro_view": MacroViewSource(StubMacroViews()),
        "brief": BriefSource(StubBriefs()),
        "fred": FredSource(fred or StubFred()),
        "sentiment": sentiment,
        "held_position": HeldPositionSource(sentiment),
    }


# --- contract: one block per Source ---------------------------------------------------


async def test_macro_view_source_reads_current_and_pins_to_as_of():
    source = MacroViewSource(StubMacroViews())
    current = await source.read(BTC, as_of=None, params=LIVE)
    assert current is not None and current.source == "macro_view"
    assert current.version == 2  # v3 is compiled after the clock
    assert "ETF inflows steady." in current.text
    assert current.as_of == NOW  # a current read is stamped with the read clock

    then = NOW - timedelta(days=1)
    pinned = await source.read(BTC, as_of=then, params=LIVE)
    assert pinned is not None and pinned.version == 1 and pinned.as_of == then
    assert await source.read(BTC, as_of=then, params=LIVE) == pinned  # stable
    assert await source.read(BTC, as_of=NOW - timedelta(days=9), params=LIVE) is None
    # The asset class picks the section.
    equity = await source.read(SPY, as_of=None, params=LIVE)
    assert equity is not None and "us_equity: Rotation." in equity.text


async def test_brief_source_reads_the_symbol_s_brief_and_pins_to_as_of():
    source = BriefSource(StubBriefs())
    current = await source.read(BTC, as_of=None, params=LIVE)
    assert current is not None and current.version == 3
    assert current.text.splitlines() == [
        "Thesis: Breakout watch.",
        "Volume rising",
        "Resistance 62k",
    ]

    then = NOW - timedelta(days=1)
    pinned = await source.read(BTC, as_of=then, params=LIVE)
    assert pinned is not None and pinned.version == 1  # SPY's v2 is another symbol's
    assert await source.read(BTC, as_of=then, params=LIVE) == pinned
    assert await source.read(BTC, as_of=NOW - timedelta(days=5), params=LIVE) is None


async def test_sentiment_source_reads_the_latest_scored_poll_and_pins_to_as_of():
    source = SentimentSource(StubSentiment())
    current = await source.read(BTC, as_of=None, params=LIVE)
    assert current is not None and current.version == 12
    assert current.text == "Latest X sentiment: +0.40 (storm) at 2026-08-12T14:00:00+00:00"

    then = NOW - timedelta(hours=2)
    pinned = await source.read(BTC, as_of=then, params=LIVE)
    assert pinned is not None and pinned.version == 10  # the error poll (id 11) is skipped
    assert await source.read(BTC, as_of=then, params=LIVE) == pinned
    assert await source.read(BTC, as_of=NOW - timedelta(days=3), params=LIVE) is None


async def test_fred_source_reads_the_print_on_or_before_the_clock_and_pins_to_as_of():
    fred = StubFred()
    source = FredSource(fred)
    current = await source.read(BTC, as_of=None, params=LIVE)
    assert current is not None and current.version == "VIXCLS@2026-08-12"
    assert "424242" not in current.text  # the print after the clock never leaks
    assert fred.calls[-1]["observation_end"] == NOW.date()

    then = NOW - timedelta(days=1)
    pinned = await source.read(BTC, as_of=then, params=LIVE)
    assert pinned is not None and pinned.version == "VIXCLS@2026-08-11"
    assert await source.read(BTC, as_of=then, params=LIVE) == pinned
    assert await FredSource(None).read(BTC, as_of=None, params=LIVE) is None  # no client
    two = await source.read(BTC, as_of=None, params={**LIVE, "series": ["VIXCLS", "DGS10"]})
    assert two is not None and two.version == "VIXCLS@2026-08-12,DGS10@2026-08-12"


async def test_held_position_source_hashes_its_render_and_pins_its_sentiment():
    source = HeldPositionSource(SentimentSource(StubSentiment()))
    params = {**LIVE, "price": 159.0, "position": HELD}
    current = await source.read(BTC, as_of=None, params=params)
    assert current is not None and current.source == "held_position"
    assert "Latest X sentiment: +0.40" in current.text
    assert isinstance(current.version, str) and len(current.version) == 16

    then = NOW - timedelta(days=1)
    pinned = await source.read(BTC, as_of=then, params=params)
    assert pinned is not None and "Latest X sentiment: -0.60" in pinned.text
    assert pinned.version != current.version
    assert await source.read(BTC, as_of=then, params=params) == pinned
    # Flat, unpriced or entry-less: nothing to review.
    assert await source.read(BTC, as_of=None, params={**params, "position": None}) is None
    assert await source.read(BTC, as_of=None, params={**params, "price": 0.0}) is None


# --- Bot #1 prompt parity --------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PARITY_SCENARIOS))
async def test_bot_one_prompts_are_byte_identical_to_the_pre_sources_build(name: str):
    scenario = PARITY_SCENARIOS[name]
    builder = ContextBuilder(
        StubMarketData(),
        sources=stub_sources(),
        subscriptions=bot_one_subscriptions(**scenario["lanes"]),
    )
    ctx = await builder.build(
        scenario["symbol"],
        now=scenario["now"],
        triggers=parity_triggers(scenario["symbol"]),
        position_qty=scenario["position_qty"],
        equity=100_000.0,
        recent_decisions=scenario["recent"],
        as_of=scenario["as_of"],
        position=scenario["position"],
    )
    assert ctx.to_prompt() == json.loads(GOLDEN.read_text())[name]


# --- the builder ------------------------------------------------------------------------


async def test_the_builder_reads_only_subscribed_sources_and_stamps_each_version():
    builder = ContextBuilder(StubMarketData(), sources=stub_sources())
    kwargs = dict(now=NOW, triggers=[], equity=100_000.0, position_qty=0.5, position=HELD)

    bare = await builder.build(BTC, **kwargs)
    assert bare.source_versions == {} and bare.macro_view is None and bare.fred_series == ()

    ctx = await builder.build(
        BTC,
        subscriptions=[Subscription("macro_view"), {"source": "brief", "params": {}}],
        **kwargs,
    )
    assert ctx.macro_view is not None and ctx.brief is not None and ctx.held_position is None
    assert ctx.source_versions == {
        "macro_view": {"version": 2, "as_of": NOW.isoformat()},
        "brief": {"version": 3, "as_of": NOW.isoformat()},
    }


async def test_an_unwired_or_failing_source_drops_its_lane_and_never_blocks_the_decide():
    class Boom:
        name = "macro_view"

        async def read(self, symbol, *, as_of, params):
            raise RuntimeError("store down")

    builder = ContextBuilder(
        StubMarketData(),
        sources={"macro_view": Boom()},
        subscriptions=[Subscription("macro_view"), Subscription("no_such_source")],
    )
    ctx = await builder.build(BTC, now=NOW, triggers=[])
    assert ctx.price > 0
    assert ctx.macro_view is None and ctx.source_versions == {}


async def test_a_bot_subscribed_to_sentiment_gets_its_own_lane_bot_one_does_not():
    builder = ContextBuilder(StubMarketData(), sources=stub_sources())
    subscribed = await builder.build(
        BTC, now=NOW, triggers=[], subscriptions=[Subscription("sentiment")]
    )
    prompt = subscribed.to_prompt()
    assert "X sentiment (advisory" in prompt and "[via sentiment #12]" in prompt
    assert "Latest X sentiment: +0.40" in prompt
    assert subscribed.source_versions["sentiment"]["version"] == 12

    bot_one = await builder.build(
        BTC, now=NOW, triggers=[], subscriptions=bot_one_subscriptions(briefs=True)
    )
    assert "X sentiment" not in bot_one.to_prompt()


# --- the Bot spec -----------------------------------------------------------------------


def settings(**kwargs) -> Settings:
    return Settings(_env_file=None, auth_disabled=True, **kwargs)


def test_bot_one_s_spec_lists_the_sources_today_s_flags_turn_on():
    dark = bot_one_spec(settings())["sources"]
    assert dark == [
        {"source": "fred", "params": {"series": ["VIXCLS"]}},
        {"source": "macro_view", "params": {}},
    ]
    lit = bot_one_spec(settings(briefs_enabled=True, exit_review_enabled=True))["sources"]
    assert [s["source"] for s in lit] == ["fred", "macro_view", "brief", "held_position"]


async def test_a_pre_sources_bot_one_moves_to_a_new_audited_version(db: Database):
    desk = BotDesk(db)
    spec = bot_one_spec(settings())
    before = {k: v for k, v in spec.items() if k != "sources"}  # what #123 seeded
    assert (await desk.seed(BOT_ONE, before))["version"] == 1

    after = await desk.seed(BOT_ONE, spec)
    assert after["version"] == 2 and after["spec"]["sources"] == spec["sources"]
    assert (await desk.seed(BOT_ONE, spec))["version"] == 2  # unchanged: nothing written
    audits = await db.fetch_all(
        "SELECT payload FROM audit_events WHERE category = 'bots' ORDER BY id"
    )
    assert [a["payload"]["version"] for a in audits] == [1, 2]
    assert audits[1]["payload"]["spec"]["sources"] == spec["sources"]


# --- the journal ------------------------------------------------------------------------


async def test_decisions_carry_source_versions_beside_the_old_version_columns(db: Database):
    view = await MacroViewsRepo(db).add(
        compiled_by="test", global_thesis="Risk-on.", sections={"crypto": "Bid."}, note_ids=[]
    )
    brief = await BriefsRepo(db).add(
        symbol="BTC/USD", compiled_by="test", thesis="Breakout.", points=[], note_ids=[]
    )
    poll = await SentimentRepo(db).insert(symbol="BTC/USD", status="ok", score=0.3)
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=1.0, avg_entry_price=150.0
    )

    runner = build_runner(db, [HOLD])
    runner.sources = bot_one_subscriptions(briefs=True, exit_review=True) + [
        {"source": "sentiment", "params": {}}
    ]
    results = await runner.tick(NOW)
    row = await Journal(db).get(results[0].decision_id)

    assert row["macro_view_version"] == view and row["brief_version"] == brief
    versions = row["source_versions"]
    assert set(versions) == {"macro_view", "brief", "sentiment", "held_position"}
    assert versions["macro_view"] == {"version": view, "as_of": NOW.isoformat()}
    assert versions["brief"]["version"] == brief
    assert versions["sentiment"]["version"] == poll
    assert "Held position review" in row["prompt"]


async def test_the_runner_defaults_its_sources_from_the_flags(db: Database):
    runner = build_runner(db, [HOLD], briefs_enabled=True)
    assert runner.sources == bot_one_subscriptions(briefs=True)


# --- replay -----------------------------------------------------------------------------


async def test_replay_reads_the_same_sources_pinned_to_each_step(db: Database):
    await seed_hourly_bars(db)
    view = await MacroViewsRepo(db).add(
        compiled_by="test", global_thesis="Then.", sections={}, note_ids=[]
    )
    await db.execute(
        "UPDATE macro_views SET created_at = %s WHERE version = %s",
        (NOW - timedelta(days=1), view),
    )
    later = await MacroViewsRepo(db).add(
        compiled_by="test", global_thesis="Now.", sections={}, note_ids=[]
    )
    await db.execute(
        "UPDATE macro_views SET created_at = %s WHERE version = %s",
        (NOW + timedelta(days=1), later),
    )
    poll = await SentimentRepo(db).insert(symbol="BTC/USD", status="ok", score=0.2)
    await db.execute(
        "UPDATE sentiment_scores SET ts = %s WHERE id = %s", (NOW - timedelta(days=1), poll)
    )

    harness = ReplayHarness(db, StubLLM([HOLD, HOLD]), StubMarketData())
    run = await harness.run(
        BTC,
        NOW - timedelta(hours=2),
        NOW,
        label="pinned",
        config={"sources": [{"source": "macro_view"}, {"source": "sentiment"}]},
    )
    assert run["status"] == "done"
    steps = await db.fetch_all("SELECT ts, prompt, decision FROM eval_steps ORDER BY ts")
    assert len(steps) == 2
    for step in steps:
        versions = step["decision"]["source_versions"]
        assert versions["macro_view"]["version"] == view
        assert versions["sentiment"]["version"] == poll
        for stamp in versions.values():
            assert datetime.fromisoformat(stamp["as_of"]) == step["ts"]  # pinned to the step
        assert "Now." not in step["prompt"] and "X sentiment (advisory" in step["prompt"]


async def test_replay_refuses_malformed_sources(db: Database):
    harness = ReplayHarness(db, StubLLM([]), StubMarketData())
    for bad in (["macro_view"], [{"params": {}}]):
        with pytest.raises(ValueError, match="sources"):
            await harness.create_run(
                BTC, NOW - timedelta(hours=1), NOW, label="bad", config={"sources": bad}
            )
    assert await db.fetch_all("SELECT * FROM eval_runs") == []


# --- the firewall -----------------------------------------------------------------------


def test_the_risk_package_never_imports_sources():
    """RiskGuard sizes from its own state, never from what a Bot read."""
    risk_dir = Path(__file__).resolve().parents[1] / "src" / "trader" / "risk"
    banned = ("trader.loop.sources", "trader.loop.context")
    for path in sorted(risk_dir.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(banned), path.name
            elif isinstance(node, ast.Import):
                assert not any(a.name.startswith(banned) for a in node.names), path.name
    # Transitively, too: importing every risk module must not pull Sources in.
    modules = [f"trader.risk.{p.stem}" for p in sorted(risk_dir.glob("*.py"))]
    code = (
        "import importlib, sys\n"
        f"for m in {modules!r}: importlib.import_module(m)\n"
        "print('trader.loop.sources' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
