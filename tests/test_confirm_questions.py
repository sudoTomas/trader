"""The confirm role's Jev judgments: question wording, the veto rule, the state."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.test_loop import BTC, NOW
from trader.core.types import Action, Bar, Decision, OrderIntent, Side
from trader.loop.confirm_questions import (
    CATALYST_MIN,
    CONTESTED_NEWS_MIN,
    GROUNDED,
    GROUNDED_MIN,
    NEWS_AGAINST,
    NEWS_AGAINST_MAX,
    QUESTIONS_VERSION,
    PositionState,
    build_state,
    evaluate,
    questions_for,
)
from trader.loop.context import DecisionContext, FredPrint, Indicators
from trader.loop.scheduler import Trigger
from trader.loop.sources import (
    BRIEF,
    LESSONS,
    MACRO_VIEW,
    SENTIMENT,
    BriefSlice,
    MacroSlice,
    SourceSlice,
)


def test_constants_are_the_agreed_values():
    assert QUESTIONS_VERSION == "2026-11-13.1"
    assert (GROUNDED_MIN, NEWS_AGAINST_MAX, CATALYST_MIN, CONTESTED_NEWS_MIN) == (
        0.25,
        0.80,
        0.25,
        0.50,
    )


# --- the veto rule ------------------------------------------------------------------------


def verdict(answers: dict[str, float], action: str = "open") -> tuple[str | None, bool]:
    result = evaluate(answers, action=action)
    return result.reason, result.would_veto


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({"grounded": 0.24, "catalyst": 0.9, "news_against": 0.1}, ("ungrounded", True)),
        ({"grounded": 0.25, "catalyst": 0.9, "news_against": 0.1}, (None, False)),
        ({"grounded": 0.9, "catalyst": 0.9, "news_against": 0.81}, ("news_against", True)),
        ({"grounded": 0.9, "catalyst": 0.9, "news_against": 0.80}, (None, False)),
        (
            {"grounded": 0.9, "catalyst": 0.24, "news_against": 0.51},
            ("generic_and_contested", True),
        ),
        ({"grounded": 0.9, "catalyst": 0.24, "news_against": 0.50}, (None, False)),
        ({"grounded": 0.9, "catalyst": 0.25, "news_against": 0.7}, (None, False)),
        ({"grounded": 0.9, "catalyst": 0.1}, (None, False)),
    ],
)
def test_rule_table_and_its_boundaries(answers, expected):
    assert verdict(answers) == expected


def test_every_row_is_evaluated_and_the_first_firing_names_the_reason():
    result = evaluate({"grounded": 0.1, "catalyst": 0.9, "news_against": 0.95}, action="open")
    assert result.would_veto is True
    assert result.reason == "ungrounded"
    assert result.rows == [
        {"reason": "ungrounded", "fired": True},
        {"reason": "news_against", "fired": True},
        {"reason": "generic_and_contested", "fired": False},
    ]


def test_missing_news_against_never_fires_the_news_rows():
    result = evaluate({"grounded": 0.9, "catalyst": 0.0}, action="open")
    assert [row["fired"] for row in result.rows] == [False, False, False]


def test_a_close_is_never_vetoed():
    result = evaluate({"grounded": 0.0, "catalyst": 0.0, "news_against": 1.0}, action="close")
    assert result.would_veto is False
    assert result.reason is None
    assert result.rows and all(row["fired"] is False for row in result.rows)


# --- the questions ------------------------------------------------------------------------


def test_open_without_research_asks_only_grounded_and_catalyst():
    assert set(questions_for("open", has_research=False, has_open_rationale=False)) == {
        "grounded",
        "catalyst",
    }


def test_open_with_research_adds_news_against_but_never_thesis_broken():
    assert set(questions_for("open", has_research=True, has_open_rationale=True)) == {
        "grounded",
        "catalyst",
        "news_against",
    }


def test_close_with_research_and_an_open_rationale_asks_all_four():
    assert set(questions_for("close", has_research=True, has_open_rationale=True)) == {
        "grounded",
        "catalyst",
        "news_against",
        "thesis_broken",
    }


def test_close_without_an_open_rationale_omits_thesis_broken():
    assert "thesis_broken" not in questions_for(
        "close", has_research=True, has_open_rationale=False
    )


def test_every_question_is_a_noul_with_both_criteria_and_never_asks_what_to_do():
    questions = questions_for("close", has_research=True, has_open_rationale=True)
    for question in questions.values():
        assert question["type"] == "noul"
        assert set(question["criteria"]) == {"true", "false"}
        assert question["criteria"]["true"] and question["criteria"]["false"]
        text = " ".join([question["instructions"], *question["criteria"].values()]).lower()
        assert "should" not in text
        assert "`" in question["instructions"]  # names the state paths it reads


# --- the state ----------------------------------------------------------------------------


def make_context(**overrides) -> DecisionContext:
    fields = {
        "symbol": BTC,
        "now": NOW,
        "triggers": [Trigger(type="price_move", symbol="BTC/USD", detail={"move_pct": 2.1})],
        "price": 61_250.0,
        "bars": {"1h": []},
        "indicators": {"1h": Indicators(rsi_14=71.2, sma_20=60_100.0, ema_12=None)},
    }
    return DecisionContext(**{**fields, **overrides})


def make_draft(action: Action = Action.OPEN, side: Side = Side.BUY) -> Decision:
    return Decision(
        symbol="BTC/USD",
        action=action,
        conviction=0.8,
        rationale="RSI 71 above SMA 20",
        intent=OrderIntent(symbol="BTC/USD", side=side, notional=500.0),
    )


def test_state_without_research_omits_the_research_key():
    state = build_state(make_context(), make_draft(), None)
    assert "research" not in state
    assert "position" not in state
    assert state["symbol"] == {"ticker": "BTC/USD", "asset_class": "crypto"}
    assert state["draft"] == {
        "action": "open",
        "side": "buy",
        "rationale": "RSI 71 above SMA 20",
        "conviction": 0.8,
    }
    assert "notional" not in state["draft"]
    assert state["market"]["price"] == 61_250.0
    assert state["market"]["indicators"]["1h"]["rsi_14"] == 71.2
    assert state["market"]["triggers"] == ["price_move: {'move_pct': 2.1}"]
    assert "fred" not in state["market"]
    json.dumps(state)


def test_state_carries_research_lanes_by_name_and_drops_empty_ones():
    as_of = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
    brief = SourceSlice(BRIEF, 3, as_of, "Thesis: ETF inflows", BriefSlice(3, ["x"]))
    sentiment = SourceSlice(SENTIMENT, 9, as_of, "")
    ctx = make_context(
        brief=brief.detail,
        sentiment=sentiment,
        sources=(brief, sentiment),
        fred_series=(FredPrint("VIXCLS", datetime(2026, 8, 11).date(), 14.2),),
    )
    state = build_state(ctx, make_draft(), None)
    assert set(state["research"]) == {"brief"}
    assert state["research"]["brief"]["text"] == "Thesis: ETF inflows"
    assert state["research"]["brief"]["as_of"] == as_of.isoformat()
    assert state["market"]["fred"] == {"VIXCLS": {"value": 14.2, "date": "2026-08-11"}}
    json.dumps(state)


def test_macro_view_and_lessons_are_not_research():
    as_of = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
    macro = SourceSlice(MACRO_VIEW, 4, as_of, "Risk-off into FOMC")
    lessons = SourceSlice(LESSONS, "l7", as_of, "Last breakout open lost 3%")
    ctx = make_context(sources=(macro, lessons), lessons=lessons)
    state = build_state(ctx, make_draft(), None)
    assert "research" not in state
    assert "news_against" not in questions_for(
        "open", has_research="research" in state, has_open_rationale=False
    )


def test_state_market_shows_the_last_closes_the_decide_prompt_shows():
    bars = [
        Bar("BTC/USD", "1h", datetime(2026, 8, 12, h, tzinfo=UTC), 1, 1, 1, 100.0 + h, 1)
        for h in range(12)
    ]
    state = build_state(make_context(bars={"1h": bars}), make_draft(), None)
    assert state["market"]["last_10_closes"]["1h"] == [100.0 + h for h in range(2, 12)]


def test_state_carries_the_position_with_its_open_rationale():
    position = PositionState(
        side="long",
        quantity=0.5,
        summary="held 3d, +4.0% unrealized",
        opened_at=datetime(2026, 8, 9, 15, 0, tzinfo=UTC),
        open_rationale="breakout above 60k on ETF inflows",
    )
    state = build_state(make_context(), make_draft(Action.CLOSE, Side.SELL), position)
    assert state["position"]["open_rationale"] == "breakout above 60k on ETF inflows"
    assert state["position"]["opened_at"] == "2026-08-09T15:00:00+00:00"
    assert state["position"]["side"] == "long"
    json.dumps(state)


def test_state_records_a_missing_open_rationale_as_null():
    position = PositionState(
        side="long", quantity=1.0, summary=None, opened_at=None, open_rationale=None
    )
    state = build_state(make_context(), make_draft(Action.CLOSE, Side.SELL), position)
    assert state["position"]["open_rationale"] is None


def test_macro_view_and_lessons_are_context_the_rationale_may_cite():
    # The decide prompt shows the Macro View and the Bot's Lessons, so a rationale that
    # cites them is grounded. They go in ``context`` (never ``research``: neither is news
    # about the symbol, so neither may feed news_against).
    as_of = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
    lessons = SourceSlice(LESSONS, "l7", as_of, "x", ["Last breakout open lost 3%"])
    ctx = make_context(
        macro_view=MacroSlice(4, ["Hyperscaler capex guidance high", "Liquidity improves Nov-Dec"]),
        lessons=lessons,
    )
    state = build_state(ctx, make_draft(), None)
    assert state["context"] == {
        "macro_view": ["Hyperscaler capex guidance high", "Liquidity improves Nov-Dec"],
        "lessons": ["Last breakout open lost 3%"],
    }
    assert "research" not in state
    json.dumps(state)


def test_state_without_macro_view_or_lessons_omits_context():
    assert "context" not in build_state(make_context(), make_draft(), None)


def test_grounded_checks_the_rationale_against_context_too():
    text = json.dumps(GROUNDED)
    assert "`context`" in text
    assert "`context`" not in json.dumps(NEWS_AGAINST)


def test_analyst_desk_reads_are_context_the_rationale_may_cite():
    # Live, the desk's analyst reads are in the decide prompt and rationales cite them
    # ("technical bullish 0.75"). Without them in the state, Jev called such opens
    # ungrounded and the veto blocked a third of Bot #1's opens on 2026-11-13.
    reads = ["technical: bullish 0.75 — price above EMA12", "research: bullish 0.70 — AI margins"]
    state = build_state(make_context(analysts=reads), make_draft(), None)
    assert state["context"] == {"analyst_desk": reads}
    assert "research" not in state
