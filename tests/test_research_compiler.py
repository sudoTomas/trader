"""EAI-178: Research Desk part 2 — Grok macro-view compiler, advisory-firewalled
injection, journal versioning.

The LLM is stubbed; the store is the real one. Anti-slop rules (from the ai-quoting
audit) are asserted deterministically: citation enforcement, price-level freezing,
stale/retired exclusion, disputed preservation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import fields
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trader.core.symbols import default_registry
from trader.llm.base import Completion, Usage
from trader.loop.context import build_macro_slice
from trader.loop.journal import Journal
from trader.ops.config import Settings
from trader.research.compiler import SYSTEM_PROMPT, MacroViewCompiler
from trader.research.notes import ResearchDesk
from trader.store.db import Database
from trader.store.repos import MacroViewsRepo, ResearchNotesRepo
from trader.surfaces.api.app import create_app

ETH = default_registry().get("ETH/USD")
DEV_SETTINGS = Settings(auth_disabled=True)


class CapturingLLM:
    """Stands in for LLMRegistry: records the (role, messages) of every call."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def complete(self, role, messages, *, schema=None, max_tokens=1024, temperature=0.2):
        self.calls.append((role, messages))
        return Completion(
            text=self.replies.pop(0),
            usage=Usage(10, 20),
            cost_usd=0.001,
            provider="stub",
            model="stub-model",
        )


def reply(thesis: str, sections: dict[str, str]) -> str:
    return json.dumps({"global_thesis": thesis, "sections": sections})


async def seed_notes(db: Database) -> dict[str, int]:
    """active + disputed (contradictory pair) + stale + retired."""
    desk = ResearchDesk(db, stale_days=30)
    bull = await desk.submit(title="ETH bull case", body="Flows are strongly positive.")
    bear = await desk.submit(title="ETH bear case", body="Flows are strongly negative.")
    await ResearchNotesRepo(db).set_status(bear["id"], "disputed")
    stale = await desk.submit(title="old take", body="EXPIRED-CONTENT", stale_days=0)
    gone = await desk.submit(title="withdrawn", body="RETIRED-CONTENT")
    await desk.retire(gone["id"])
    return {"bull": bull["id"], "bear": bear["id"], "stale": stale["id"], "gone": gone["id"]}


# --- compiler ---------------------------------------------------------------


async def test_compile_prompt_carries_live_notes_only_and_preserves_dispute(db: Database):
    ids = await seed_notes(db)
    llm = CapturingLLM(
        [
            reply(
                f"Contradictory flow reads persist [note:{ids['bull']}] [note:{ids['bear']}]",
                {"crypto": f"Bull vs bear unresolved [note:{ids['bull']}] [note:{ids['bear']}]"},
            )
        ]
    )
    view = await MacroViewCompiler(db, llm).compile_now()
    assert view is not None

    (role, messages) = llm.calls[0]
    assert role == "compiler"
    prompt = "\n".join(m["content"] for m in messages)
    # Both live notes are in, labeled with id and status; disputed is never dropped.
    assert f"note:{ids['bull']}" in prompt and "active" in prompt
    assert f"note:{ids['bear']}" in prompt and "disputed" in prompt
    # Stale and retired notes never reach the compiler.
    assert "EXPIRED-CONTENT" not in prompt and "RETIRED-CONTENT" not in prompt
    # The anti-slop contract is spelled out to the model.
    assert "cite" in prompt.lower()
    assert "disputed" in prompt.lower()

    # Both contradictory notes survive into the stored view.
    assert set(view["note_ids"]) == {ids["bull"], ids["bear"]}
    assert str(ids["bull"]) in view["sections"]["crypto"]
    assert str(ids["bear"]) in view["sections"]["crypto"]


async def test_single_asset_notes_yield_an_empty_global_thesis_and_no_foreign_lane(db: Database):
    """EAI-186 shadow-review finding: with only ETH-scoped notes the compiler wrote an
    ETH-flavored "global" thesis, which the global lane then rendered into SPY prompts.
    The contract now: global_thesis carries only cross-asset claims (empty when the
    notes are single-asset); asset-class sections already scope correctly."""
    assert "cross-asset" in SYSTEM_PROMPT
    assert "empty" in SYSTEM_PROMPT

    desk = ResearchDesk(db)
    note = await desk.submit(title="ETH flows", body="inflows", asset_classes=["crypto"])
    llm = CapturingLLM([reply("", {"crypto": f"Inflow pattern [note:{note['id']}]"})])
    view = await MacroViewCompiler(db, llm).compile_now()
    assert view is not None and view["global_thesis"] == ""

    # The crypto lane renders its section; other asset classes get NO lane at all.
    crypto = build_macro_slice(view, "crypto", budget_chars=2000)
    assert crypto is not None and "Inflow pattern" in "\n".join(crypto.entries)
    assert build_macro_slice(view, "us_equity", budget_chars=2000) is None


async def test_compiler_sees_authored_dates_and_is_told_recency_means_authorship(db: Database):
    """The recency the compiler weighs is when the research was CREATED, not submitted —
    an old article captured today must not read as fresh."""
    from datetime import UTC, datetime, timedelta

    desk = ResearchDesk(db)
    aged = await desk.submit(
        title="aged",
        body="old take",
        authored_at=datetime.now(tz=UTC) - timedelta(days=20),
    )
    fresh = await desk.submit(title="fresh", body="new take")
    llm = CapturingLLM([reply(f"t [note:{aged['id']}] [note:{fresh['id']}]", {})])
    assert await MacroViewCompiler(db, llm).compile_now() is not None

    prompt = "\n".join(m["content"] for m in llm.calls[0][1])
    assert "authored" in prompt.lower()
    assert prompt.lower().count("authored:") == 2  # every note line carries it
    # The dated line distinguishes authorship from submission for the aged note.
    aged_id = aged["id"]
    aged_line = next(ln for ln in prompt.splitlines() if f"note:{aged_id}" in ln)
    assert "authored:" in aged_line and "submitted:" in aged_line


async def test_price_levels_are_frozen_as_example_not_current(db: Database):
    desk = ResearchDesk(db)
    note = await desk.submit(title="levels", body="Support held twice.")
    llm = CapturingLLM(
        [
            reply(
                f"Support pattern at $2,400 keeps holding [note:{note['id']}]",
                {"crypto": f"Watch the $2,400.50 retest pattern [note:{note['id']}]"},
            )
        ]
    )
    view = await MacroViewCompiler(db, llm).compile_now()
    assert "$2,400 (example-not-current)" in view["global_thesis"]
    assert "$2,400.50 (example-not-current)" in view["sections"]["crypto"]


async def test_sections_without_valid_citations_are_dropped(db: Database):
    desk = ResearchDesk(db)
    note = await desk.submit(title="one", body="content")
    llm = CapturingLLM(
        [
            reply(
                f"Cited thesis [note:{note['id']}]",
                {
                    "crypto": f"cited [note:{note['id']}]",
                    "us_equity": "uncited slop the model made up",
                    "commodity": "cites a ghost [note:999]",
                },
            )
        ]
    )
    view = await MacroViewCompiler(db, llm).compile_now()
    assert set(view["sections"]) == {"crypto"}


async def test_compile_versions_increment_and_audit(db: Database):
    desk = ResearchDesk(db)
    note = await desk.submit(title="n", body="b")
    cited = f"thesis [note:{note['id']}]"
    llm = CapturingLLM([reply(cited, {}), reply(cited, {})])
    compiler = MacroViewCompiler(db, llm)

    first = await compiler.compile_now()
    second = await compiler.compile_now()
    assert (first["version"], second["version"]) == (1, 2)
    assert second["compiled_by"] == "stub:stub-model"

    events = await db.fetch_all(
        "SELECT action, subject FROM audit_events"
        " WHERE category = 'research' AND action = 'compile_view' ORDER BY id"
    )
    assert [e["subject"] for e in events] == ["macro_view:1", "macro_view:2"]


async def test_no_notes_means_no_view(db: Database):
    llm = CapturingLLM([])
    assert await MacroViewCompiler(db, llm).compile_now() is None
    assert await MacroViewsRepo(db).current() is None
    assert llm.calls == []


async def test_mark_dirty_debounces_to_one_compile(db: Database):
    desk = ResearchDesk(db)
    note = await desk.submit(title="n", body="b")
    llm = CapturingLLM([reply(f"t [note:{note['id']}]", {})] * 3)
    compiler = MacroViewCompiler(db, llm, debounce_seconds=0.05)

    compiler.mark_dirty()
    compiler.mark_dirty()
    compiler.mark_dirty()
    await asyncio.sleep(0.3)
    assert len(llm.calls) == 1
    assert (await MacroViewsRepo(db).current())["version"] == 1


# --- advisory firewall + injection ------------------------------------------


def view_row(version: int = 1, thesis: str = "Risk-on tone", sections: dict | None = None) -> dict:
    return {
        "version": version,
        "global_thesis": thesis,
        "sections": {"crypto": "Flows positive [note:1]"} if sections is None else sections,
        "note_ids": [1],
    }


def test_macro_slice_renders_for_the_symbols_asset_class():
    slice_ = build_macro_slice(view_row(), "crypto", budget_chars=2000)
    assert slice_ is not None and slice_.version == 1
    joined = "\n".join(slice_.entries)
    assert "Risk-on tone" in joined and "Flows positive" in joined

    other = build_macro_slice(view_row(), "us_equity", budget_chars=2000)
    assert "Flows positive" not in "\n".join(other.entries)  # other lane's section


def test_macro_slice_budget_drops_entries_whole():
    row = view_row(sections={"crypto": "S" * 5000})
    slice_ = build_macro_slice(row, "crypto", budget_chars=100)
    joined = "\n".join(slice_.entries)
    assert "Risk-on tone" in joined
    assert "SSS" not in joined  # oversize entry dropped whole, never truncated mid-thought

    assert build_macro_slice({**row, "global_thesis": ""}, "crypto", budget_chars=100) is None


def test_riskguard_is_structurally_firewalled_from_research():
    """RiskGuard cannot receive the Macro View: no parameter or field can carry it."""
    from trader.risk.guard import RiskContext, RiskGuard

    surface = [p.lower() for p in inspect.signature(RiskGuard.evaluate).parameters]
    surface += [f.name.lower() for f in fields(RiskContext)]
    for name in surface:
        assert "macro" not in name and "research" not in name and "view" not in name


# --- journal versioning through the loop ------------------------------------

HOLD = '{"action":"hold","conviction":0.3,"rationale":"nothing doing"}'


async def test_decision_journals_the_macro_view_version_it_saw(db: Database):
    from tests.test_loop import NOW, build_runner

    version = await MacroViewsRepo(db).add(
        compiled_by="stub:stub-model",
        global_thesis="Risk-on [note:1]",
        sections={"crypto": "Flows positive [note:1]"},
        note_ids=[1],
    )

    results = await build_runner(db, [HOLD]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["macro_view_version"] == version
    assert "Operator Macro View" in row["prompt"]
    assert "advisory" in row["prompt"]
    assert "Risk-on" in row["prompt"]


async def test_decision_without_a_view_journals_no_version_and_no_lane(db: Database):
    from tests.test_loop import NOW, build_runner

    results = await build_runner(db, [HOLD]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["macro_view_version"] is None
    assert "Macro View" not in row["prompt"]


# --- surfaces ---------------------------------------------------------------


def test_macro_view_surfaced_on_api(clean_db: None):
    async def seed() -> int:
        database = Database(min_size=1, max_size=1)
        await database.open()
        try:
            return await MacroViewsRepo(database).add(
                compiled_by="stub:stub-model",
                global_thesis="Thesis [note:1]",
                sections={"crypto": "S [note:1]"},
                note_ids=[1],
            )
        finally:
            await database.close()

    version = asyncio.run(seed())
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        body = tc.get("/research/view").json()
    assert body["version"] == version
    assert body["global_thesis"] == "Thesis [note:1]"
    assert body["note_ids"] == [1]


def test_macro_view_api_empty_when_never_compiled(clean_db: None):
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        assert tc.get("/research/view").json() is None


def test_cli_macro_view(clean_db: None, monkeypatch: pytest.MonkeyPatch):
    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(DEV_SETTINGS)))
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["macro", "view"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) is None  # empty store → null, not an error
