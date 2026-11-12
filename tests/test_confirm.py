"""Confirm seat: Jev answers atomic Noul judgments about every open or close Draft, and a
code-side rule (loop/confirm_questions.py) decides whether an open would be vetoed.

Order on a non-hold Draft: Draft, Confirm, critic, RiskGuard. The Confirmer returns the
record for the decision's ``confirm`` column; the record says whether the open is vetoed
(``vetoed``), and the loop applies it. Jev down, slow, erroring or malformed is a
journaled skip and never a veto.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tests.test_desk import RoleLLM, analyst_reply, critic_reply
from tests.test_loop import BTC, HOLD, NOW, OPEN, build_runner, context_for
from trader.core.types import Action, Decision, OrderIntent, Side
from trader.llm.base import Completion, LLMError, Usage
from trader.llm.jev import JevClient
from trader.llm.registry import LLMRegistry, NoRoute
from trader.loop.confirm import Confirmer
from trader.loop.confirm_questions import (
    QUESTIONS_VERSION,
    PositionState,
    build_state,
    questions_for,
)
from trader.loop.context import DecisionContext, Indicators
from trader.loop.journal import Journal
from trader.loop.scheduler import Trigger
from trader.loop.sources import BRIEF, SENTIMENT, BriefSlice, SourceSlice

GOLDEN = Path(__file__).parent / "golden" / "confirm_open_request.json"


def noul(**answers: float) -> dict:
    return {qid: {"type": "noul", "noul": p} for qid, p in answers.items()}


SOUND = noul(grounded=0.9, catalyst=0.8)  # an open the rule passes
UNGROUNDED = noul(grounded=0.1, catalyst=0.8)  # an open the rule vetoes


class FakeJev:
    """LLMRegistry stand-in: records each call and answers with ``reply`` (a dict, sent
    as JSON) or raises it."""

    def __init__(self, reply: dict | Exception, model: str = "jev-1.15") -> None:
        self.reply = reply
        self.model = model
        self.calls: list[dict] = []

    async def complete(self, role, messages, *, schema=None, state=None, **kwargs):
        self.calls.append({"role": role, "messages": messages, "schema": schema, "state": state})
        if isinstance(self.reply, Exception):
            raise self.reply
        return Completion(
            text=json.dumps(self.reply),
            usage=Usage(prompt_tokens=900, completion_tokens=4),
            cost_usd=0.0,
            provider="jev",
            model=self.model,
        )


def jev(seen: list, *, answers=None, status: int = 200, error: Exception | None = None, **kw):
    """A Confirmer on a real JevClient behind a mock transport; ``seen`` logs request bodies."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if error is not None:
            raise error
        if status != 200:
            return httpx.Response(status, text="secret-bearing body")
        payload = {"model": "jev-1.15", "answers": SOUND if answers is None else answers}
        return httpx.Response(200, json={**payload, "usage": {}})

    client = JevClient(
        api_key="k",
        model="jev-latest",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return Confirmer(LLMRegistry({"confirm": [client]}, None), **kw)


def make_draft(action: Action = Action.OPEN, side: Side = Side.BUY) -> Decision:
    return Decision(
        symbol="BTC/USD",
        action=action,
        conviction=0.8,
        rationale="momentum",
        intent=OrderIntent(symbol="BTC/USD", side=side, notional=500.0),
    )


AS_OF = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)


def researched_context() -> DecisionContext:
    """A fixed context with one research lane, for the request snapshot."""
    brief = SourceSlice(BRIEF, 3, AS_OF, "Thesis: ETF inflows steady", BriefSlice(3, ["x"]))
    sentiment = SourceSlice(SENTIMENT, 9, AS_OF, "X sentiment +0.4, volume high")
    return DecisionContext(
        symbol=BTC,
        now=NOW,
        triggers=[Trigger(type="price_move", symbol="BTC/USD", detail={"move_pct": 2.1})],
        price=61_250.0,
        bars={"1h": []},
        indicators={"1h": Indicators(rsi_14=71.2, sma_20=60_100.0, ema_12=None)},
        brief=brief.detail,
        sentiment=sentiment,
        sources=(brief, sentiment),
    )


HELD = PositionState(
    side="long",
    quantity=0.5,
    summary=None,
    opened_at=datetime(2026, 8, 9, 15, 0, tzinfo=UTC),
    open_rationale="breakout above 60k on ETF inflows",
)


# --- the Confirmer ------------------------------------------------------------------------


async def test_a_vetoed_open_records_the_veto():
    record = await Confirmer(FakeJev(UNGROUNDED)).confirm(await context_for(), make_draft())
    latency = record.pop("latency_ms")
    assert isinstance(latency, int) and latency >= 0
    assert record == {
        "draft": {"action": "open", "side": "buy"},
        "answers": {"grounded": 0.1, "catalyst": 0.8},
        "rule": [
            {"reason": "ungrounded", "fired": True},
            {"reason": "news_against", "fired": False},
            {"reason": "generic_and_contested", "fired": False},
        ],
        "would_veto": True,
        "veto_reason": "ungrounded",
        "vetoed": True,
        "verdict": "veto",
        "questions_version": QUESTIONS_VERSION,
        "model": "jev-1.15",
        "skipped": None,
    }


async def test_with_the_switch_off_a_failing_open_is_recorded_but_passes():
    confirmer = Confirmer(FakeJev(UNGROUNDED), veto_enabled=False)
    record = await confirmer.confirm(await context_for(), make_draft())
    assert (record["would_veto"], record["veto_reason"]) == (True, "ungrounded")
    assert (record["vetoed"], record["verdict"]) == (False, "pass")


async def test_a_sound_open_passes():
    record = await Confirmer(FakeJev(SOUND)).confirm(await context_for(), make_draft())
    assert (record["would_veto"], record["vetoed"], record["verdict"]) == (False, False, "pass")
    assert record["veto_reason"] is None


async def test_a_close_is_recorded_never_vetoed():
    fake = FakeJev(noul(grounded=0.0, catalyst=0.0))
    record = await Confirmer(fake).confirm(await context_for(), make_draft(Action.CLOSE, Side.SELL))
    assert record["draft"] == {"action": "close", "side": "sell"}
    assert (record["would_veto"], record["vetoed"], record["verdict"]) == (
        False,
        False,
        "recorded",
    )


async def test_jev_gets_the_structured_state_and_the_questions_for_the_draft():
    fake = FakeJev(SOUND)
    ctx = await context_for()
    draft = make_draft()
    await Confirmer(fake).confirm(ctx, draft)
    [call] = fake.calls
    assert call["role"] == "confirm"
    assert call["messages"] == []  # Jev reads the state, not a chat
    assert call["state"] == build_state(ctx, draft, None)
    assert call["state"]["draft"]["rationale"] == "momentum"
    assert call["schema"] == questions_for("open", has_research=False, has_open_rationale=False)


async def test_research_adds_news_against():
    fake = FakeJev(noul(grounded=0.9, catalyst=0.8, news_against=0.95))
    record = await Confirmer(fake).confirm(researched_context(), make_draft())
    assert set(fake.calls[0]["schema"]) == {"grounded", "catalyst", "news_against"}
    assert "research" in fake.calls[0]["state"]
    assert (record["veto_reason"], record["vetoed"]) == ("news_against", True)


async def test_a_close_with_an_open_rationale_asks_thesis_broken():
    fake = FakeJev(noul(grounded=0.9, catalyst=0.8, thesis_broken=0.7))
    draft = make_draft(Action.CLOSE, Side.SELL)
    record = await Confirmer(fake).confirm(await context_for(), draft, HELD)
    assert set(fake.calls[0]["schema"]) == {"grounded", "catalyst", "thesis_broken"}
    assert fake.calls[0]["state"]["position"]["open_rationale"] == HELD.open_rationale
    assert record["answers"]["thesis_broken"] == 0.7


async def test_a_close_without_an_open_rationale_omits_thesis_broken():
    fake = FakeJev(noul(grounded=0.9, catalyst=0.8))
    draft = make_draft(Action.CLOSE, Side.SELL)
    for rationale in (None, "", "  "):
        held = PositionState("long", 0.5, None, None, rationale)
        await Confirmer(fake).confirm(await context_for(), draft, held)
        assert set(fake.calls[-1]["schema"]) == {"grounded", "catalyst"}


async def test_the_open_request_matches_the_snapshot():
    seen: list = []
    await jev(seen).confirm(researched_context(), make_draft())
    assert seen[0] == json.loads(GOLDEN.read_text())


@pytest.mark.parametrize(
    "answers",
    [
        noul(grounded=0.9),  # catalyst missing
        {"grounded": {"type": "noul", "noul": 0.9}, "catalyst": {"type": "noul"}},
        noul(grounded=0.9, catalyst=1.7),
        noul(grounded=-0.1, catalyst=0.5),
        noul(grounded=math.nan, catalyst=0.5),
        {"grounded": {"type": "noul", "noul": "high"}, "catalyst": {"type": "noul", "noul": 0.5}},
        {"grounded": {"type": "noul", "noul": True}, "catalyst": {"type": "noul", "noul": 0.5}},
        {"grounded": 0.9, "catalyst": 0.5},
        [],
    ],
)
async def test_malformed_answers_are_a_skip_and_never_a_veto(answers):
    record = await Confirmer(FakeJev(answers)).confirm(await context_for(), make_draft())
    assert set(record) == {"draft", "skipped", "vetoed"}
    assert record["skipped"].startswith("malformed answers")
    assert record["vetoed"] is False


async def test_a_blind_context_never_reaches_jev():
    """The state is not rendered through the blind view: it names the symbol, its prices
    and dates. A blinded replay step is a skip, never a Jev call."""
    from dataclasses import replace

    fake = FakeJev(UNGROUNDED)
    ctx = replace(await context_for(), blind=object())
    record = await Confirmer(fake).confirm(ctx, make_draft())
    assert fake.calls == []
    assert record == {
        "draft": {"action": "open", "side": "buy"},
        "skipped": "blind",
        "vetoed": False,
    }


async def test_no_route_records_nothing():
    assert (
        await Confirmer(FakeJev(NoRoute("confirm"))).confirm(await context_for(), make_draft())
        is None
    )


async def test_a_provider_error_is_a_skip_with_its_status_text():
    record = await Confirmer(FakeJev(LLMError("jev: HTTP 500"))).confirm(
        await context_for(), make_draft()
    )
    assert record == {
        "draft": {"action": "open", "side": "buy"},
        "skipped": "HTTP 500",
        "vetoed": False,
    }


async def test_unreachable_jev_is_a_skip_with_the_status_text():
    record = await jev([], error=httpx.ConnectTimeout("boom")).confirm(
        await context_for(), make_draft()
    )
    assert record == {
        "draft": {"action": "open", "side": "buy"},
        "skipped": "unreachable (ConnectTimeout)",
        "vetoed": False,
    }


async def test_http_500_is_a_skip_and_never_stores_the_body():
    record = await jev([], status=500).confirm(await context_for(), make_draft())
    assert record["skipped"] == "HTTP 500"
    assert "secret" not in json.dumps(record)


async def test_jev_missing_an_answer_is_a_malformed_skip():
    record = await jev([], answers=noul(grounded=0.9)).confirm(await context_for(), make_draft())
    assert record["skipped"].startswith("malformed answers")
    assert record["vetoed"] is False


# --- the settings ---------------------------------------------------------------------------


def test_the_veto_switch_defaults_on_and_reads_the_env(monkeypatch):
    from trader.ops.config import Settings
    from trader.ops.config_reference import collect_fields

    assert Settings(_env_file=None).confirm_veto_enabled is True
    monkeypatch.setenv("TRADER_CONFIRM_VETO_ENABLED", "false")
    assert Settings(_env_file=None).confirm_veto_enabled is False
    fields = {field.env_name: field for field in collect_fields()}
    assert fields["TRADER_CONFIRM_VETO_ENABLED"].category == "gate"


# --- the runner -----------------------------------------------------------------------------


async def test_non_hold_draft_journals_a_confirm(db):
    results = await build_runner(db, [OPEN], confirm=jev([])).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["confirm"]["verdict"] == "pass"
    assert row["confirm"]["answers"] == {"grounded": 0.9, "catalyst": 0.8}
    assert row["confirm"]["draft"] == {"action": "open", "side": "buy"}


async def test_hold_draft_never_reaches_jev(db):
    seen: list = []
    results = await build_runner(db, [HOLD], confirm=jev(seen)).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["confirm"] is None
    assert seen == []


async def test_no_confirm_configured_writes_no_column(db):
    results = await build_runner(db, [OPEN]).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["confirm"] is None
    assert row["decision"]["action"] == "open"


async def test_jev_failures_skip_and_the_draft_reaches_the_critic_unchanged(db):
    from trader.desk.desk import Desk

    for confirmer, reason in (
        (jev([], error=httpx.ConnectError("refused")), "unreachable (ConnectError)"),
        (jev([], status=500), "HTTP 500"),
        (jev([], answers=noul(grounded=0.9)), "malformed answers: KeyError"),
    ):
        llm = RoleLLM(
            {"analyst": [analyst_reply("bullish", 0.7, "up")], "critic": [critic_reply("approve")]}
        )
        results = await build_runner(db, [OPEN], desk=Desk(llm), confirm=confirmer).tick(NOW)
        row = await Journal(db).get(results[0].decision_id)
        assert row["confirm"] == {
            "draft": {"action": "open", "side": "buy"},
            "skipped": reason,
            "vetoed": False,
        }
        assert row["desk_verdicts"]["critic"]["verdict"] == "approve"
        assert row["desk_verdicts"]["applied"]["notional_before"] == 500.0
        assert row["risk_verdicts"] is not None


async def test_a_crashing_confirmer_is_a_skip(db):
    class Exploding:
        async def confirm(self, ctx, decision):
            raise RuntimeError("jev on fire")

    results = await build_runner(db, [OPEN], confirm=Exploding()).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert row["confirm"]["skipped"] == "error (RuntimeError)"
    assert row["confirm"]["vetoed"] is False
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
            "confirm": [json.dumps(SOUND)],
        }
    )
    await build_runner(db, [OPEN], desk=Desk(llm), confirm=Confirmer(llm)).tick(NOW)
    assert order == ["analyst", "confirm", "critic"]


async def test_with_the_veto_off_confirm_never_alters_the_decision(db):
    """Switch off: whatever Jev says, the Draft the critic and RiskGuard see is the decide
    output, object for object, and the record only says the rule would have vetoed."""
    seen_by_critic: list[Decision] = []

    class SpyDesk:
        async def enrich(self, ctx):
            return ctx, {}

        async def review(self, ctx, decision, info):
            seen_by_critic.append(decision)
            return decision, info

    confirmer = jev([], answers=UNGROUNDED, veto_enabled=False)
    runner = build_runner(db, [OPEN], desk=SpyDesk(), confirm=confirmer)
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
    assert (row["confirm"]["would_veto"], row["confirm"]["vetoed"]) == (True, False)
    assert row["decision"]["action"] == "open"
    assert row["decision"]["intent"]["notional"] == 500.0
    assert row["risk_verdicts"] is not None


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
