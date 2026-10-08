"""Confirm seat, first stage (#99): Kev judges every open or close Draft, journal-only.

Order on a non-hold Draft: Draft, Confirm, critic, RiskGuard. The Confirm's answer goes
into the decision's own ``confirm`` column and nothing reads it back: in this stage it can
never change the decision. Kev down, slow, erroring or malformed is a journaled skip.
"""

from __future__ import annotations

import json

import httpx

from tests.test_desk import RoleLLM, analyst_reply, critic_reply
from tests.test_loop import HOLD, NOW, OPEN, build_runner, context_for
from trader.core.types import Action, Decision, OrderIntent, Side
from trader.llm.jev import JevClient
from trader.llm.registry import LLMRegistry
from trader.loop.confirm import JOURNAL_ONLY, QUESTIONS, Confirmer
from trader.loop.decider import SYSTEM_PROMPT
from trader.loop.journal import Journal

KEV_DOUBTS = {
    "action": {"choice": "hold", "probabilities": {"hold": 0.61, "open": 0.31, "close": 0.08}},
    "open_side": {"choice": "buy", "probabilities": {"buy": 0.7, "sell": 0.3}},
}
KEV_AGREES = {
    "action": {"choice": "open", "probabilities": {"hold": 0.2, "open": 0.72, "close": 0.08}},
    "open_side": {"choice": "buy", "probabilities": {"buy": 0.8, "sell": 0.2}},
}


def kev(seen: list, *, answers=None, status: int = 200, error: Exception | None = None):
    """A Confirmer on a real JevClient behind a mock transport; ``seen`` logs request bodies."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if error is not None:
            raise error
        if status != 200:
            return httpx.Response(status, text="secret-bearing body")
        payload = {"answers": KEV_DOUBTS if answers is None else answers, "usage": {}}
        return httpx.Response(200, json=payload)

    client = JevClient(
        api_key="k",
        model="kev-latest",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return Confirmer(LLMRegistry({"confirm": [client]}, None))


def make_draft(action: Action = Action.OPEN, side: Side = Side.BUY) -> Decision:
    return Decision(
        symbol="BTC/USD",
        action=action,
        conviction=0.8,
        rationale="momentum",
        intent=OrderIntent(symbol="BTC/USD", side=side, notional=500.0),
    )


# --- the Confirmer ------------------------------------------------------------------------


async def test_confirm_record_has_the_journal_shape():
    seen: list = []
    record = await kev(seen).confirm(await context_for(), make_draft())
    latency = record.pop("latency_ms")
    assert isinstance(latency, int) and latency >= 0
    assert record == {
        "draft": {"action": "open", "side": "buy"},
        "action": {"hold": 0.61, "open": 0.31, "close": 0.08},
        "side": {"buy": 0.7, "sell": 0.3},
        "p_same_action": 0.31,
        "prefers": "hold",
        "margin": None,
        "verdict": JOURNAL_ONLY,
        "model": "kev-latest",
        "skipped": None,
    }


async def test_confirm_sends_the_decide_prompt_the_draft_and_the_rules():
    seen: list = []
    ctx = await context_for()
    await kev(seen).confirm(ctx, make_draft())
    state = seen[0]["state"]
    assert state["prompt"].startswith(ctx.to_prompt())
    assert "Draft decision under review:" in state["prompt"]
    assert '"notional_usd": 500.0' in state["prompt"]
    assert state["rules"] == SYSTEM_PROMPT
    # The action choice plus the side question for the Draft's own action.
    assert seen[0]["questions"] == {
        "action": QUESTIONS["action"],
        "open_side": QUESTIONS["open_side"],
    }


async def test_a_close_draft_asks_the_close_side():
    seen: list = []
    answers = {
        "action": {"probabilities": {"hold": 0.3, "open": 0.0, "close": 0.7}},
        "close_side": {"probabilities": {"sell": 0.2, "buy": 0.8}},
    }
    record = await kev(seen, answers=answers).confirm(
        await context_for(), make_draft(Action.CLOSE, Side.SELL)
    )
    assert set(seen[0]["questions"]) == {"action", "close_side"}
    assert record["draft"] == {"action": "close", "side": "sell"}
    assert record["p_same_action"] == 0.7
    assert record["prefers"] == "other_side"  # same action, the other side


async def test_agreement_prefers_the_drafts_own_action():
    record = await kev([], answers=KEV_AGREES).confirm(await context_for(), make_draft())
    assert (record["prefers"], record["p_same_action"]) == ("open", 0.72)


async def test_unreachable_kev_is_a_skip_with_the_status_text():
    record = await kev([], error=httpx.ConnectTimeout("boom")).confirm(
        await context_for(), make_draft()
    )
    assert record == {
        "draft": {"action": "open", "side": "buy"},
        "skipped": "unreachable (ConnectTimeout)",
    }


async def test_http_500_is_a_skip_and_never_stores_the_body():
    record = await kev([], status=500).confirm(await context_for(), make_draft())
    assert record["skipped"] == "HTTP 500"
    assert "secret" not in json.dumps(record)


async def test_malformed_answers_are_a_skip():
    for answers in ({"action": {"choice": "hold"}}, {"action": {"probabilities": {}}}, {}):
        record = await kev([], answers=answers).confirm(await context_for(), make_draft())
        assert set(record) == {"draft", "skipped"}
        assert record["skipped"].startswith("malformed answers")


# --- the runner -----------------------------------------------------------------------------


async def test_non_hold_draft_journals_a_confirm(db):
    results = await build_runner(db, [OPEN], confirm=kev([])).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["confirm"]["verdict"] == JOURNAL_ONLY
    assert row["confirm"]["action"] == {"hold": 0.61, "open": 0.31, "close": 0.08}
    assert row["confirm"]["draft"] == {"action": "open", "side": "buy"}


async def test_hold_draft_never_reaches_kev(db):
    seen: list = []
    results = await build_runner(db, [HOLD], confirm=kev(seen)).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["confirm"] is None
    assert seen == []


async def test_no_confirm_configured_writes_no_column(db):
    results = await build_runner(db, [OPEN]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["confirm"] is None
    assert row["decision"]["action"] == "open"


async def test_kev_failures_skip_and_the_draft_reaches_the_critic_unchanged(db):
    from trader.desk.desk import Desk

    for confirmer, reason in (
        (kev([], error=httpx.ConnectError("refused")), "unreachable (ConnectError)"),
        (kev([], status=500), "HTTP 500"),
        (kev([], answers={"action": {"choice": "hold"}}), "malformed answers: KeyError"),
    ):
        llm = RoleLLM(
            {"analyst": [analyst_reply("bullish", 0.7, "up")], "critic": [critic_reply("approve")]}
        )
        results = await build_runner(db, [OPEN], desk=Desk(llm), confirm=confirmer).tick(NOW)
        row = await Journal(db).get(results[0].decision_id)
        assert row["confirm"] == {"draft": {"action": "open", "side": "buy"}, "skipped": reason}
        assert row["desk_verdicts"]["critic"]["verdict"] == "approve"
        assert row["desk_verdicts"]["applied"]["notional_before"] == 500.0
        assert row["risk_verdicts"] is not None


async def test_a_crashing_confirmer_is_a_skip(db):
    class Exploding:
        async def confirm(self, ctx, decision):
            raise RuntimeError("kev on fire")

    results = await build_runner(db, [OPEN], confirm=Exploding()).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["confirm"]["skipped"] == "error (RuntimeError)"
    assert row["decision"]["action"] == "open"


async def test_confirm_runs_before_the_critic(db):
    from trader.desk.desk import Desk

    order: list[str] = []

    class Recording(RoleLLM):
        async def complete(self, role, messages, **kwargs):
            order.append(role)
            return await super().complete(role, messages, **kwargs)

    llm = Recording(
        {
            "analyst": [analyst_reply("bullish", 0.7, "up")],
            "critic": [critic_reply("approve")],
            "confirm": [json.dumps(KEV_AGREES)],
        }
    )
    await build_runner(db, [OPEN], desk=Desk(llm), confirm=Confirmer(llm)).tick(NOW)
    assert order == ["analyst", "confirm", "critic"]


async def test_confirm_can_never_alter_the_decision(db):
    """The power ceiling of this stage: whatever Kev says, the Draft the critic and
    RiskGuard see is the decide output, object for object."""
    seen_by_critic: list[Decision] = []

    class SpyDesk:
        async def enrich(self, ctx):
            return ctx, {}

        async def review(self, ctx, decision, info):
            seen_by_critic.append(decision)
            return decision, info

    certain_hold = {
        "action": {"probabilities": {"hold": 1.0, "open": 0.0, "close": 0.0}},
        "open_side": {"probabilities": {"buy": 0.0, "sell": 1.0}},
    }
    runner = build_runner(db, [OPEN], desk=SpyDesk(), confirm=kev([], answers=certain_hold))
    decided: list[Decision] = []
    decide = runner._decider.decide

    async def spy_decide(ctx):
        result = await decide(ctx)
        decided.append(result.decision)
        return result

    runner._decider.decide = spy_decide
    results = await runner.tick(NOW)
    row = await Journal(db).get(results[0].decision_id)

    assert seen_by_critic[0] is decided[0]
    assert row["confirm"]["prefers"] == "hold"
    assert row["decision"]["action"] == "open"
    assert row["decision"]["intent"]["notional"] == 500.0
    assert row["risk_verdicts"] is not None
    # Structurally: the Confirmer returns a record, and the record has no field to apply.
    assert set(row["confirm"]) == {
        "draft",
        "action",
        "side",
        "p_same_action",
        "prefers",
        "margin",
        "verdict",
        "model",
        "latency_ms",
        "skipped",
    }


async def test_the_app_confirmer_records_nothing_until_a_confirm_route_exists(db):
    """The app always builds a Confirmer (a Profile can add the Role at runtime, #107);
    with no .env Jev and no Profile Chain it returns no record, so the column stays NULL."""
    from trader.ops.config import Settings
    from trader.surfaces.api.app import build_runner as build_app_runner

    keys = {"alpaca_api_key_id": "id", "alpaca_api_secret": "secret"}  # shadow: never dialed
    plain = build_app_runner(db, Settings(_env_file=None, **keys))
    ctx = await context_for()
    assert isinstance(plain.confirm, Confirmer)
    assert await plain.confirm.confirm(ctx, make_draft()) is None
    configured = build_app_runner(
        db, Settings(_env_file=None, confirm_provider="jev", confirm_api_key="k", **keys)
    )
    assert configured.llm.chain_for("confirm")[0].model == "jev-latest"
    assert [c.name for c in configured.llm.chain_for("confirm")] == ["jev"]
