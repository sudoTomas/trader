"""EAI-190: the decision desk — analyst fan-out feeding the decider as labeled lanes,
and a cross-model critic between decide and RiskGuard that can only approve, shrink, or
veto. The LLMs are stubbed; the store is the real one. Dark behind TRADER_DESK_ENABLED.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from tests.test_loop import context_for
from trader.llm.base import Completion, LLMError, Usage


class RoleLLM:
    """LLMRegistry stand-in with per-role scripted replies; records (role, messages)."""

    def __init__(self, replies: dict[str, list[str | Exception]]) -> None:
        self.replies = {role: list(seq) for role, seq in replies.items()}
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def complete(self, role, messages, *, schema=None, max_tokens=1024, temperature=0.2):
        self.calls.append((role, messages))
        reply = self.replies[role].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Completion(
            text=reply,
            usage=Usage(prompt_tokens=100, completion_tokens=20),
            cost_usd=0.01,
            provider="xai",
            model="grok-4",
        )


def analyst_reply(stance: str, confidence: float, note: str) -> str:
    return json.dumps({"stance": stance, "confidence": confidence, "note": note})


# --- analyst lanes in the prompt ---------------------------------------------


async def test_analyst_lanes_render_as_their_own_advisory_block():
    base = await context_for()
    assert "Analyst desk" not in base.to_prompt()

    ctx = replace(base, analysts=["[technical] bullish (0.60): RSI rising off the mid-band"])
    prompt = ctx.to_prompt()
    assert "Analyst desk" in prompt
    assert "advisory" in prompt
    assert "[technical] bullish (0.60): RSI rising off the mid-band" in prompt


# --- analyst pool ------------------------------------------------------------


async def test_technical_always_runs_and_research_joins_when_lanes_present():
    from trader.desk.analysts import AnalystPool
    from trader.loop.context import MacroSlice

    llm = RoleLLM(
        {
            "analyst": [
                analyst_reply("bullish", 0.6, "momentum building"),
                analyst_reply("bearish", 0.4, "macro says chop"),
            ]
        }
    )
    ctx = replace(
        await context_for(),
        macro_view=MacroSlice(version=3, entries=["Global thesis: risk-on but crowded"]),
    )
    reads = await AnalystPool(llm).analyze(ctx)

    assert [r["lane"] for r in reads] == ["technical", "research"]
    assert all(r["ok"] for r in reads)
    assert reads[0]["stance"] == "bullish" and reads[1]["stance"] == "bearish"

    # Both ran on the analyst role, each with a focused system prompt over the SAME
    # already-assembled context — no new I/O.
    assert [role for role, _ in llm.calls] == ["analyst", "analyst"]
    technical_system = llm.calls[0][1][0]["content"]
    research_system = llm.calls[1][1][0]["content"]
    assert technical_system != research_system
    for _, messages in llm.calls:
        assert "BTC/USD" in messages[1]["content"]  # the shared context prompt


async def test_research_analyst_skipped_without_research_lanes():
    from trader.desk.analysts import AnalystPool

    llm = RoleLLM({"analyst": [analyst_reply("neutral", 0.2, "quiet tape")]})
    reads = await AnalystPool(llm).analyze(await context_for())  # no macro, no brief
    assert [r["lane"] for r in reads] == ["technical"]
    assert len(llm.calls) == 1


async def test_failed_and_invalid_analysts_drop_their_lane_without_blocking():
    from trader.desk.analysts import AnalystPool
    from trader.loop.context import MacroSlice

    llm = RoleLLM({"analyst": [LLMError("endpoint down"), "not json at all"]})
    ctx = replace(
        await context_for(),
        macro_view=MacroSlice(version=1, entries=["Global thesis: risk-off"]),
    )
    reads = await AnalystPool(llm).analyze(ctx)
    assert [r["lane"] for r in reads] == ["technical", "research"]
    assert [r["ok"] for r in reads] == [False, False]
    assert "error" in reads[0]


async def test_reads_render_as_labeled_lane_lines():
    from trader.desk.analysts import render_reads

    reads = [
        {"lane": "technical", "ok": True, "stance": "bullish", "confidence": 0.6, "note": "up"},
        {"lane": "research", "ok": False, "error": "endpoint down"},
    ]
    assert render_reads(reads) == ["[technical] bullish (0.60): up"]  # failed lanes drop


# --- critic ------------------------------------------------------------------


def critic_reply(verdict: str, notional: float | None = None, reason: str = "reviewed") -> str:
    return json.dumps({"verdict": verdict, "notional_usd": notional, "reason": reason})


def make_open(notional: float = 500.0):
    from trader.core.types import Action, Decision, OrderIntent, Side

    return Decision(
        symbol="BTC/USD",
        action=Action.OPEN,
        conviction=0.8,
        rationale="momentum",
        intent=OrderIntent(symbol="BTC/USD", side=Side.BUY, notional=notional),
    )


def make_hold():
    from trader.core.types import Action, Decision

    return Decision(symbol="BTC/USD", action=Action.HOLD, conviction=0.3, rationale="quiet")


async def test_critic_approve_leaves_the_decision_unchanged():
    from trader.desk.desk import Desk

    llm = RoleLLM({"critic": [critic_reply("approve", reason="sized sanely")]})
    final, info = await Desk(llm).review(await context_for(), make_open(), {"analysts": []})
    assert final == make_open()
    assert info["critic"]["verdict"] == "approve"
    assert info["applied"] == {
        "action_before": "open",
        "notional_before": 500.0,
        "action_after": "open",
        "notional_after": 500.0,
    }
    # The critic saw both the context and the draft decision.
    (role, messages) = llm.calls[0]
    assert role == "critic"
    assert "BTC/USD" in messages[1]["content"] and "momentum" in messages[1]["content"]


async def test_critic_shrink_reduces_notional():
    from trader.desk.desk import Desk

    llm = RoleLLM({"critic": [critic_reply("shrink", notional=300.0, reason="too big for vol")]})
    final, info = await Desk(llm).review(await context_for(), make_open(500.0), {"analysts": []})
    assert final.intent is not None and final.intent.notional == 300.0
    assert final.action.value == "open" and final.intent.side.value == "buy"  # never redirected
    assert info["applied"]["notional_after"] == 300.0


async def test_critic_shrink_can_never_enlarge():
    from trader.desk.desk import Desk

    llm = RoleLLM({"critic": [critic_reply("shrink", notional=900.0, reason="add more")]})
    final, info = await Desk(llm).review(await context_for(), make_open(500.0), {"analysts": []})
    assert final.intent is not None and final.intent.notional == 500.0  # clamped to original
    assert info["critic"]["clamped"] is True


async def test_critic_veto_converts_to_hold():
    from trader.core.types import Action
    from trader.desk.desk import Desk

    llm = RoleLLM({"critic": [critic_reply("veto", reason="thesis contradicts the tape")]})
    final, info = await Desk(llm).review(await context_for(), make_open(), {"analysts": []})
    assert final.action is Action.HOLD and final.intent is None
    assert final.conviction == 0.8  # draft's conviction survives for the journal
    assert info["critic"]["verdict"] == "veto"
    assert info["applied"]["action_after"] == "hold"


async def test_critic_unavailable_is_a_journaled_pass_through():
    from trader.desk.desk import Desk

    llm = RoleLLM({"critic": [LLMError("all providers failed")]})
    final, info = await Desk(llm).review(await context_for(), make_open(), {"analysts": []})
    assert final == make_open()
    assert info["critic"]["verdict"] == "pass_through"
    assert "unavailable" in info["critic"]["reason"]


async def test_critic_invalid_output_is_a_journaled_pass_through():
    from trader.desk.desk import Desk

    llm = RoleLLM({"critic": ["utter nonsense, not json"]})
    final, info = await Desk(llm).review(await context_for(), make_open(), {"analysts": []})
    assert final == make_open()
    assert info["critic"]["verdict"] == "pass_through"


async def test_holds_never_reach_the_critic():
    from trader.desk.desk import Desk

    llm = RoleLLM({"critic": []})  # any call would pop from an empty list and fail
    final, info = await Desk(llm).review(await context_for(), make_hold(), {"analysts": []})
    assert final == make_hold()
    assert info["critic"]["verdict"] == "skipped"
    assert llm.calls == []


async def test_critic_daily_budget_cap_passes_through(db):
    from datetime import UTC, datetime

    from trader.desk.desk import Desk
    from trader.store.repos import UsageRepo

    usage = UsageRepo(db)
    await usage.record(
        day=datetime.now(tz=UTC).date(),
        role="critic",
        provider="xai",
        model="grok-4",
        prompt_tokens=1000,
        completion_tokens=100,
        cost_usd=0.02,
    )
    llm = RoleLLM({"critic": []})  # the wire must not be touched
    desk = Desk(llm, usage=usage, critic_daily_usd=0.01)
    final, info = await desk.review(await context_for(), make_open(), {"analysts": []})
    assert final == make_open()
    assert info["critic"]["verdict"] == "pass_through"
    assert "budget" in info["critic"]["reason"]
    assert llm.calls == []


def test_critic_wire_schema_requires_every_key():
    """Same failure class as the decider's omitted side/notional: a shrink verdict
    without its notional would pass guided decoding if the key were optional."""
    from trader.desk.critic import CRITIC_SCHEMA

    assert set(CRITIC_SCHEMA["required"]) == set(CRITIC_SCHEMA["properties"])


def test_technical_analyst_stance_is_earned_not_the_default():
    """EAI-190 gate finding: the technical analyst read neutral on 330/331 steps — no
    signal. The prompt now reserves neutral for genuinely conflicting indicators."""
    from trader.desk.analysts import TECHNICAL_PROMPT

    assert "conflict" in TECHNICAL_PROMPT.lower()
    assert "lean" in TECHNICAL_PROMPT.lower()


def test_critic_output_is_structurally_unable_to_redirect():
    """No field exists for side, symbol, or a larger size — enlarging/redirecting is
    impossible by schema, not by prompt."""
    from trader.desk.critic import CriticOutput

    assert set(CriticOutput.model_json_schema()["properties"]) == {
        "verdict",
        "notional_usd",
        "reason",
    }


async def test_desk_enrich_attaches_analyst_lanes_to_the_context():
    from trader.desk.desk import Desk

    llm = RoleLLM({"analyst": [analyst_reply("bullish", 0.7, "clean uptrend")]})
    ctx, info = await Desk(llm).enrich(await context_for())
    assert ctx.analysts == ["[technical] bullish (0.70): clean uptrend"]
    assert "Analyst desk" in ctx.to_prompt()
    assert info["analysts"][0]["lane"] == "technical"


# --- config + routing ---------------------------------------------------------


def test_settings_desk_defaults_dark_with_a_critic_budget():
    from trader.ops.config import Settings

    settings = Settings(auth_disabled=True)
    assert settings.desk_enabled is False
    assert settings.desk_critic_daily_usd > 0


def test_registry_routes_analyst_local_and_critic_cross_model():
    from trader.llm.registry import ROLES, build_llm_registry
    from trader.ops.config import Settings

    assert {"analyst", "critic"} <= set(ROLES)

    with_grok = build_llm_registry(Settings(auth_disabled=True, xai_api_key="k"))
    # Analysts run every wakeup: local-only, free by definition.
    assert [c.name for c in with_grok.chain_for("analyst")] == ["nemotron"]
    # The critic is cross-model (Grok reviews Nemotron's draft), Nemotron backstop.
    assert [c.name for c in with_grok.chain_for("critic")] == ["xai", "nemotron"]

    without = build_llm_registry(Settings(auth_disabled=True))
    assert [c.name for c in without.chain_for("critic")] == ["nemotron"]


# --- runner integration + journal ---------------------------------------------

OPEN = '{"action":"open","conviction":0.8,"rationale":"momentum","side":"buy","notional_usd":500}'


async def test_desk_verdicts_journal_with_the_decision_when_desk_is_on(db):
    from tests.test_loop import NOW, build_runner
    from trader.desk.desk import Desk
    from trader.loop.journal import Journal

    desk = Desk(
        RoleLLM(
            {
                "analyst": [analyst_reply("bullish", 0.7, "clean uptrend")],
                "critic": [critic_reply("shrink", notional=200.0, reason="half size in chop")],
            }
        )
    )
    results = await build_runner(db, [OPEN], desk=desk).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)

    assert "Analyst desk" in row["prompt"]  # the decider saw the lanes
    verdicts = row["desk_verdicts"]
    assert verdicts["analysts"][0]["lane"] == "technical"
    assert verdicts["critic"]["verdict"] == "shrink"
    assert verdicts["applied"] == {
        "action_before": "open",
        "notional_before": 500.0,
        "action_after": "open",
        "notional_after": 200.0,
    }
    # RiskGuard evaluated the SHRUNK intent — the desk shaped what reached the chokepoint.
    assert row["risk_verdicts"] is not None


async def test_desk_veto_journals_shadow_hold_with_the_reason(db):
    from tests.test_loop import NOW, build_runner
    from trader.desk.desk import Desk
    from trader.loop.journal import SHADOW, Journal

    desk = Desk(
        RoleLLM(
            {
                "analyst": [analyst_reply("bearish", 0.8, "distribution")],
                "critic": [critic_reply("veto", reason="rationale unsupported")],
            }
        )
    )
    results = await build_runner(db, [OPEN], desk=desk).tick(NOW)
    assert results[0].status == SHADOW

    row = await Journal(db).get(results[0].decision_id)
    assert row["desk_verdicts"]["critic"]["reason"] == "rationale unsupported"
    assert row["desk_verdicts"]["applied"]["action_after"] == "hold"
    assert row["risk_verdicts"] is None  # a vetoed intent never reaches the guard
    assert await db.fetch_all("SELECT * FROM orders") == []


async def test_desk_is_dark_by_default(db):
    from tests.test_loop import NOW, build_runner
    from trader.loop.journal import Journal

    results = await build_runner(db, [OPEN]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["desk_verdicts"] is None
    assert "Analyst desk" not in row["prompt"]


async def test_a_crashing_desk_never_blocks_the_decide(db):
    from tests.test_loop import NOW, build_runner
    from trader.loop.journal import SHADOW, Journal

    class ExplodingDesk:
        async def enrich(self, ctx):
            raise RuntimeError("desk on fire")

        async def review(self, ctx, decision, info):
            raise RuntimeError("still on fire")

    results = await build_runner(db, [OPEN], desk=ExplodingDesk()).tick(NOW)
    assert results[0].status == SHADOW  # the wakeup completed without the desk
    row = await Journal(db).get(results[0].decision_id)
    assert row["decision"]["action"] == "open"


# --- replay variant plumbing (the EAI-188 gate) -------------------------------


async def test_replay_desk_on_tallies_verdicts_and_journals_desk_steps(db):
    from datetime import timedelta

    from tests.test_loop import StubLLM, StubMarketData
    from tests.test_replay_eval import BTC, NOW, seed_hourly_bars
    from trader.desk.desk import Desk
    from trader.eval.replay import ReplayHarness

    await seed_hourly_bars(db)
    open_ = (
        '{"action":"open","conviction":0.8,"rationale":"momentum","side":"buy","notional_usd":1000}'
    )
    hold = '{"action":"hold","conviction":0.3,"rationale":"nothing doing"}'
    close = (
        '{"action":"close","conviction":0.7,"rationale":"take profit",'
        '"side":"sell","notional_usd":1000}'
    )
    desk = Desk(
        RoleLLM(
            {
                "analyst": [analyst_reply("bullish", 0.6, "up")] * 3,
                "critic": [
                    critic_reply("shrink", notional=400.0, reason="chop"),
                    critic_reply("approve", reason="clean exit"),
                ],
            }
        )
    )
    harness = ReplayHarness(db, StubLLM([open_, hold, close]), StubMarketData(), desk=desk)
    run = await harness.run(
        BTC, NOW - timedelta(hours=3), NOW, label="desk-on", config={"desk": True}
    )

    assert run["status"] == "done"
    assert run["summary"]["desk"] == {
        "approve": 1,
        "shrink": 1,
        "veto": 0,
        "pass_through": 0,
        "skipped": 1,  # the hold step
    }
    steps = await db.fetch_all("SELECT * FROM eval_steps ORDER BY ts")
    open_step = steps[0]["decision"]
    assert open_step["desk"]["critic"]["verdict"] == "shrink"
    assert open_step["desk"]["applied"]["notional_after"] == 400.0
    assert open_step["desk"]["analysts"][0]["lane"] == "technical"


async def test_replay_without_desk_config_stays_baseline(db):
    from datetime import timedelta

    from tests.test_loop import StubLLM, StubMarketData
    from tests.test_replay_eval import BTC, NOW, seed_hourly_bars
    from trader.eval.replay import ReplayHarness

    await seed_hourly_bars(db)
    hold = '{"action":"hold","conviction":0.3,"rationale":"nothing doing"}'
    harness = ReplayHarness(db, StubLLM([hold]), StubMarketData())
    run = await harness.run(BTC, NOW - timedelta(hours=1), NOW, label="baseline")
    assert run["status"] == "done"
    assert "desk" not in run["summary"]


async def test_replay_desk_requested_without_desk_fails_loud(db):
    from datetime import timedelta

    import pytest

    from tests.test_loop import StubLLM, StubMarketData
    from tests.test_replay_eval import BTC, NOW
    from trader.eval.replay import ReplayHarness

    harness = ReplayHarness(db, StubLLM([]), StubMarketData())
    with pytest.raises(ValueError, match="desk"):
        await harness.create_run(
            BTC, NOW - timedelta(hours=1), NOW, label="desk-on", config={"desk": True}
        )


async def test_run_replay_eval_service_forwards_the_desk_flag(db):
    from trader.surfaces.services import build_registry

    class RecordingEvaluator:
        def __init__(self) -> None:
            self.created: list[dict[str, Any]] = []

        async def create_run(self, symbol, start, end, *, label, config=None):
            self.created.append({"label": label, "config": config})
            return 7

        async def execute(self, run_id, symbol):
            return {}

    evaluator = RecordingEvaluator()
    registry = build_registry(db, evaluator=evaluator)
    out = await registry.get("run_replay_eval").handler(
        symbol="BTC/USD",
        start="2026-08-01T00:00:00+00:00",
        end="2026-08-02T00:00:00+00:00",
        label="desk-on",
        desk=True,
    )
    assert out["run_id"] == 7
    assert evaluator.created[0]["config"] == {"desk": True}
