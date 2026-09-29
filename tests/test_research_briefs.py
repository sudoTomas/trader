"""Per-candidate research briefs — Grok `brief` role compiles symbol-scoped
notes + the latest ok sentiment into a versioned per-symbol digest; the decide prompt
gets a budgeted lane behind TRADER_BRIEFS_ENABLED (dark by default).

The LLM is stubbed; the store is the real one. The Macro View anti-slop rules are
asserted deterministically: citation enforcement, price-level freezing, no-inputs →
no brief (see docs/lessons.md#no-inputs-no-brief).
"""

from __future__ import annotations

import asyncio
import json

from tests.test_research_compiler import CapturingLLM
from trader.research.briefs import BriefCompiler
from trader.research.notes import ResearchDesk
from trader.store.db import Database
from trader.store.repos import BriefsRepo, SentimentRepo


def brief_reply(thesis: str, points: list[str] | None = None) -> str:
    return json.dumps({"thesis": thesis, "points": points or []})


async def seed_inputs(db: Database) -> dict[str, int]:
    """ETH-scoped + SPY-scoped + unscoped notes; old/fresh ok + degraded sentiment."""
    desk = ResearchDesk(db)
    eth = await desk.submit(
        title="ETH flows", body="ETH-NOTE-CONTENT etf inflows strong.", symbols=["ETH/USD"]
    )
    spy = await desk.submit(
        title="SPY breadth", body="SPY-NOTE-CONTENT breadth thin.", symbols=["SPY"]
    )
    general = await desk.submit(title="macro", body="GENERAL-NOTE-CONTENT rates story.")
    sentiment = SentimentRepo(db)
    old = await sentiment.insert(
        symbol="ETH/USD",
        status="ok",
        score=0.1,
        volume="quiet",
        drivers=["old-driver"],
        citations=["https://x.com/a"],
        model="stub-model",
    )
    fresh = await sentiment.insert(
        symbol="ETH/USD",
        status="ok",
        score=0.6,
        volume="elevated",
        drivers=["etf-flows-driver"],
        citations=["https://x.com/b"],
        model="stub-model",
    )
    await sentiment.insert(
        symbol="ETH/USD", status="unavailable", model="stub-model", reason="degraded"
    )
    return {
        "eth": eth["id"],
        "spy": spy["id"],
        "general": general["id"],
        "old": old,
        "fresh": fresh,
    }


# --- compiler ---------------------------------------------------------------


async def test_compile_carries_symbol_scoped_notes_and_latest_ok_sentiment(db: Database):
    ids = await seed_inputs(db)
    llm = CapturingLLM(
        [
            brief_reply(
                f"Inflow pattern intact [note:{ids['eth']}] [sent:{ids['fresh']}]",
                [f"Sentiment elevated on ETF flows [sent:{ids['fresh']}]"],
            )
        ]
    )
    brief = await BriefCompiler(db, llm, symbols=["ETH/USD"]).compile_now("ETH/USD")
    assert brief is not None

    (role, messages) = llm.calls[0]
    assert role == "brief"
    prompt = "\n".join(m["content"] for m in messages)
    # Only the symbol's own notes qualify — the macro lane already covers the rest.
    assert "ETH-NOTE-CONTENT" in prompt
    assert "SPY-NOTE-CONTENT" not in prompt and "GENERAL-NOTE-CONTENT" not in prompt
    # The latest OK sentiment rides along, labeled; degraded rows never feed a brief.
    assert f"sent:{ids['fresh']}" in prompt and "etf-flows-driver" in prompt
    assert "old-driver" not in prompt
    # The anti-slop contract is spelled out to the model.
    assert "cite" in prompt.lower()

    assert brief["symbol"] == "ETH/USD"
    assert brief["note_ids"] == [ids["eth"]]
    assert brief["sentiment_id"] == ids["fresh"]
    assert brief["compiled_by"] == "stub:stub-model"


async def test_sentiment_alone_is_a_valid_input(db: Database):
    sentiment_id = await SentimentRepo(db).insert(
        symbol="ETH/USD",
        status="ok",
        score=-0.4,
        volume="normal",
        drivers=["fee-fud"],
        citations=["https://x.com/c"],
        model="stub-model",
    )
    llm = CapturingLLM([brief_reply(f"Fee FUD weighing [sent:{sentiment_id}]")])
    brief = await BriefCompiler(db, llm, symbols=["ETH/USD"]).compile_now("ETH/USD")
    assert brief is not None
    assert brief["note_ids"] == [] and brief["sentiment_id"] == sentiment_id


async def test_uncited_thesis_drops_the_whole_brief(db: Database):
    await seed_inputs(db)
    llm = CapturingLLM([brief_reply("Confident slop with no citations")])
    compiler = BriefCompiler(db, llm, symbols=["ETH/USD"])
    assert await compiler.compile_now("ETH/USD") is None
    assert await BriefsRepo(db).current("ETH/USD") is None


async def test_uncited_or_ghost_cited_points_drop_individually(db: Database):
    ids = await seed_inputs(db)
    llm = CapturingLLM(
        [
            brief_reply(
                f"Thesis holds [note:{ids['eth']}]",
                [
                    f"cited fine [note:{ids['eth']}]",
                    "uncited slop",
                    "ghost citation [note:9999]",
                    f"wrong-lane citation [note:{ids['spy']}]",
                ],
            )
        ]
    )
    brief = await BriefCompiler(db, llm, symbols=["ETH/USD"]).compile_now("ETH/USD")
    assert brief["points"] == [f"cited fine [note:{ids['eth']}]"]


async def test_price_levels_are_frozen_as_example_not_current(db: Database):
    ids = await seed_inputs(db)
    llm = CapturingLLM(
        [
            brief_reply(
                f"Support pattern at $2,400 held [note:{ids['eth']}]",
                [f"Watch the $2,400.50 retest pattern [note:{ids['eth']}]"],
            )
        ]
    )
    brief = await BriefCompiler(db, llm, symbols=["ETH/USD"]).compile_now("ETH/USD")
    assert "$2,400 (example-not-current)" in brief["thesis"]
    assert "$2,400.50 (example-not-current)" in brief["points"][0]


async def test_no_symbol_scoped_inputs_means_no_brief_and_no_llm_call(db: Database):
    desk = ResearchDesk(db)
    await desk.submit(title="macro", body="unscoped")  # feeds the Macro View, not a brief
    llm = CapturingLLM([])
    compiler = BriefCompiler(db, llm, symbols=["ETH/USD"])
    assert await compiler.compile_now("ETH/USD") is None
    assert llm.calls == []
    assert await BriefsRepo(db).current("ETH/USD") is None


async def test_compile_versions_increment_and_audit(db: Database):
    ids = await seed_inputs(db)
    cited = brief_reply(f"thesis [note:{ids['eth']}]")
    llm = CapturingLLM([cited, cited])
    compiler = BriefCompiler(db, llm, symbols=["ETH/USD"])

    first = await compiler.compile_now("ETH/USD")
    second = await compiler.compile_now("ETH/USD")
    assert (first["version"], second["version"]) == (1, 2)

    events = await db.fetch_all(
        "SELECT subject FROM audit_events"
        " WHERE category = 'research' AND action = 'compile_brief' ORDER BY id"
    )
    assert [e["subject"] for e in events] == ["brief:1", "brief:2"]


async def test_mark_dirty_debounces_and_compiles_only_symbols_with_inputs(db: Database):
    ids = await seed_inputs(db)
    llm = CapturingLLM([brief_reply(f"t [note:{ids['eth']}]")] * 3)
    # SPY note exists but BTC has no inputs: one ETH compile, no BTC call, one LLM call
    # total after a burst of dirty marks.
    compiler = BriefCompiler(db, llm, symbols=["ETH/USD", "BTC/USD"], debounce_seconds=0.05)

    compiler.mark_dirty()
    compiler.mark_dirty()
    compiler.mark_dirty()
    await asyncio.sleep(0.3)
    assert len(llm.calls) == 1
    assert (await BriefsRepo(db).current("ETH/USD"))["version"] == 1
    assert await BriefsRepo(db).current("BTC/USD") is None


# --- lane + journal + firewall ----------------------------------------------


def brief_row(
    version: int = 1, thesis: str = "Inflows intact [note:1]", points: list[str] | None = None
) -> dict:
    return {
        "version": version,
        "symbol": "BTC/USD",
        "thesis": thesis,
        "points": ["Sentiment elevated [sent:2]"] if points is None else points,
    }


def test_brief_slice_renders_thesis_and_points():
    from trader.loop.context import build_brief_slice

    slice_ = build_brief_slice(brief_row(), budget_chars=2000)
    assert slice_ is not None and slice_.version == 1
    joined = "\n".join(slice_.entries)
    assert "Inflows intact" in joined and "Sentiment elevated" in joined


def test_brief_slice_budget_drops_entries_whole():
    from trader.loop.context import build_brief_slice

    slice_ = build_brief_slice(brief_row(points=["S" * 5000]), budget_chars=100)
    joined = "\n".join(slice_.entries)
    assert "Inflows intact" in joined
    assert "SSS" not in joined  # oversize entry dropped whole, never truncated mid-thought

    assert build_brief_slice(brief_row(thesis="T" * 5000, points=[]), budget_chars=100) is None


def test_riskguard_is_structurally_firewalled_from_briefs():
    """RiskGuard cannot receive a brief: no parameter or field can carry it."""
    import inspect
    from dataclasses import fields

    from trader.risk.guard import RiskContext, RiskGuard

    surface = [p.lower() for p in inspect.signature(RiskGuard.evaluate).parameters]
    surface += [f.name.lower() for f in fields(RiskContext)]
    for name in surface:
        assert "brief" not in name


HOLD = '{"action":"hold","conviction":0.3,"rationale":"nothing doing"}'


async def test_decision_journals_the_brief_version_it_saw_when_enabled(db: Database):
    from tests.test_loop import NOW, build_runner
    from trader.loop.journal import Journal

    version = await BriefsRepo(db).add(
        symbol="BTC/USD",
        compiled_by="stub:stub-model",
        thesis="Inflows intact [note:1]",
        points=["Sentiment elevated [sent:2]"],
        note_ids=[1],
        sentiment_id=None,
    )

    results = await build_runner(db, [HOLD], briefs_enabled=True).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["brief_version"] == version
    assert "Candidate Brief" in row["prompt"]
    assert "advisory" in row["prompt"]
    assert f"[via brief v{version}]" in row["prompt"]
    assert "Inflows intact" in row["prompt"]


async def test_briefs_lane_is_dark_by_default(db: Database):
    from tests.test_loop import NOW, build_runner
    from trader.loop.journal import Journal

    await BriefsRepo(db).add(
        symbol="BTC/USD",
        compiled_by="stub:stub-model",
        thesis="Inflows intact [note:1]",
        points=[],
        note_ids=[1],
        sentiment_id=None,
    )
    results = await build_runner(db, [HOLD]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["brief_version"] is None
    assert "Candidate Brief" not in row["prompt"]


# --- surfaces ---------------------------------------------------------------


def test_brief_capabilities_are_registered():
    from trader.surfaces.services import build_registry

    tools = {c.tool_name for c in build_registry(None).all()}
    assert {"get_brief", "compile_brief"} <= tools


def test_settings_briefs_lane_defaults_dark():
    from trader.ops.config import Settings

    assert Settings(auth_disabled=True).briefs_enabled is False


def test_brief_parity_across_all_surfaces(clean_db: None, monkeypatch):
    """The same brief must come back identical through every door."""
    import json as jsonlib

    import pytest  # noqa: F401
    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    async def seed_and_query_direct() -> tuple[dict, dict]:
        database = Database(min_size=1, max_size=1)
        await database.open()
        try:
            await BriefsRepo(database).add(
                symbol="ETH/USD",
                compiled_by="stub:stub-model",
                thesis="Inflow pattern intact [note:1]",
                points=["Sentiment elevated [sent:2]"],
                note_ids=[1],
                sentiment_id=None,
            )
            registry = build_registry(database)
            via_registry = await registry.get("brief").handler(symbol="ETH/USD")
            async with Client(build_mcp(registry)) as mcp_client:
                call = await mcp_client.call_tool("get_brief", {"symbol": "ETH/USD"})
            return via_registry, call.structured_content["result"]
        finally:
            await database.close()

    import asyncio as aio

    via_registry, via_mcp = aio.run(seed_and_query_direct())

    dev = Settings(auth_disabled=True)
    with TestClient(create_app(dev)) as tc:
        via_api = tc.get("/research/briefs", params={"symbol": "ETH/USD"}).json()

    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(dev)))
    result = CliRunner().invoke(cli.app, ["brief", "show", "ETH/USD"])
    assert result.exit_code == 0, result.output
    via_cli = jsonlib.loads(result.output)

    assert via_registry["version"] == 1
    assert via_registry["thesis"] == "Inflow pattern intact [note:1]"
    assert via_registry == via_api == via_mcp == via_cli


def test_compile_brief_door_reports_no_inputs(clean_db: None):
    """POST the compile door with an empty store: honest refusal, no LLM involved."""
    from fastapi.testclient import TestClient

    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app

    with TestClient(create_app(Settings(auth_disabled=True))) as tc:
        out = tc.post("/research/briefs/compile", params={"symbol": "ETH/USD"}).json()
    assert out["compiled"] is False
    assert "input" in out["reason"]


async def test_note_changes_mark_briefs_dirty(db: Database):
    from trader.surfaces.services import build_registry

    class RecordingCompiler:
        def __init__(self) -> None:
            self.marks = 0

        def mark_dirty(self) -> None:
            self.marks += 1

    briefs = RecordingCompiler()
    registry = build_registry(db, briefs=briefs)
    note = await registry.get("submit_research_note").handler(
        title="ETH flows", body="inflows", symbols=["ETH/USD"]
    )
    assert briefs.marks == 1
    await registry.get("retire_note").handler(note_id=note["id"])
    assert briefs.marks == 2


# --- store ------------------------------------------------------------------


async def test_briefs_repo_versions_globally_and_tracks_current_per_symbol(db: Database):
    repo = BriefsRepo(db)
    v1 = await repo.add(
        symbol="ETH/USD",
        compiled_by="stub:stub-model",
        thesis="Flows positive [note:1]",
        points=["Momentum building [note:1]"],
        note_ids=[1],
        sentiment_id=None,
    )
    v2 = await repo.add(
        symbol="ETH/USD",
        compiled_by="stub:stub-model",
        thesis="Flows fading [note:2]",
        points=[],
        note_ids=[2],
        sentiment_id=None,
    )
    v3 = await repo.add(
        symbol="SPY",
        compiled_by="stub:stub-model",
        thesis="Breadth improving [note:3]",
        points=[],
        note_ids=[3],
        sentiment_id=None,
    )
    # Version is a single global identity (like macro_views) — the journal points at it.
    assert (v1, v2, v3) == (1, 2, 3)

    current = await repo.current("ETH/USD")
    assert current is not None and current["version"] == v2
    assert current["thesis"] == "Flows fading [note:2]"
    assert (await repo.current("SPY"))["version"] == v3
    assert await repo.current("BTC/USD") is None

    row = await repo.get(v1)
    assert row is not None
    assert row["symbol"] == "ETH/USD"
    assert row["points"] == ["Momentum building [note:1]"]
    assert row["note_ids"] == [1]
    assert row["sentiment_id"] is None
