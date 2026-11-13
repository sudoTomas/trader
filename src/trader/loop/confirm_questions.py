"""The confirm role's Jev judgments: the questions, the veto rule and the state they read.

Jev answers small, observable judgments about named state fields; code holds the decision
(docs/superpowers/specs/2026-11-12-confirm-jev-judgments-design.md). The wording, the
thresholds and the rule live together here so a change to one is reviewed beside the
others, and ``QUESTIONS_VERSION`` tells journaled question sets apart.

Each answer is Jev's calibrated p(true) for a Noul question. Only an **open** can be
vetoed; a close is judged and recorded, never vetoed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trader.core.types import Decision
from trader.loop.context import DecisionContext
from trader.loop.sources import BRIEF, FRED, HELD_POSITION, LESSONS, MACRO_VIEW, SourceSlice

QUESTIONS_VERSION = "2026-11-13.1"

GROUNDED_MIN = 0.25  # veto an open when p(grounded) is below this
NEWS_AGAINST_MAX = 0.80  # veto an open when p(news_against) is above this
CATALYST_MIN = 0.25  # with CONTESTED_NEWS_MIN: a generic rationale against contested news
CONTESTED_NEWS_MIN = 0.50

CLOSES_PER_INTERVAL = 10  # the decide prompt shows the last 10 closes per interval

# Lanes that are not research: FRED prints are market facts (``market.fred``), the
# held-position block is the ``position`` field, the Macro View is not symbol-scoped, and
# Lessons are the Bot's own reflections, not news (neither may feed ``news_against``).
# The decide prompt still shows both, so they go in ``context`` for ``grounded`` to check.
_NOT_RESEARCH = frozenset({FRED, HELD_POSITION, MACRO_VIEW, LESSONS})


# --- the questions ------------------------------------------------------------------------


def _noul(instructions: str, true: str, false: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"true": true, "false": false},
    }


GROUNDED = _noul(
    "Does every number and fact stated in `draft.rationale` appear in `market`, `research`"
    " or `context`?",
    "Each number, level and event the rationale cites can be found in `market`, `research`"
    " or `context`.",
    "The rationale cites a number or event that `market`, `research` and `context` do not show.",
)
CATALYST = _noul(
    "Does `draft.rationale` name a specific signal or event behind the trade?",
    "The rationale names a concrete signal or event, such as an indicator level, a price"
    " move or a news item.",
    "The rationale uses only generic language, such as 'momentum looks good' or"
    " 'favourable setup', without naming what it refers to.",
)
NEWS_AGAINST = _noul(
    "Does `research` give a reason to expect `symbol` to move opposite to `draft.side`?",
    "`research` reports news, sentiment or a forecast pointing the other way from"
    " `draft.side`: down for a buy, up for a sell.",
    "Everything in `research` points the same way as `draft.side` or carries no"
    " direction for `symbol`.",
)
THESIS_BROKEN = _noul(
    "Does something in `market` or `research` since `position.opened_at` contradict"
    " `position.open_rationale`?",
    "A price level, indicator or news item now in `market` or `research` contradicts a"
    " reason given in `position.open_rationale`.",
    "The reasons in `position.open_rationale` still match what `market` and `research` show now.",
)


def questions_for(
    action: str, *, has_research: bool, has_open_rationale: bool
) -> dict[str, dict[str, Any]]:
    """The Noul questions for one non-hold Draft, keyed by judgment id.

    ``news_against`` needs research to read; ``thesis_broken`` is asked of a close whose
    opening rationale was recovered.
    """
    questions = {"grounded": GROUNDED, "catalyst": CATALYST}
    if has_research:
        questions["news_against"] = NEWS_AGAINST
    if action == "close" and has_open_rationale:
        questions["thesis_broken"] = THESIS_BROKEN
    return questions


# --- the veto rule ------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleResult:
    would_veto: bool
    reason: str | None  # the first firing row's reason code
    rows: list[dict[str, Any]]  # [{"reason": str, "fired": bool}], every row evaluated


def evaluate(answers: dict[str, float], *, action: str) -> RuleResult:
    """Apply the veto table to Jev's answers. A missing ``news_against`` fires no row that
    reads it; a non-open Draft fires nothing."""
    grounded = answers.get("grounded")
    catalyst = answers.get("catalyst")
    news = answers.get("news_against")
    is_open = action == "open"
    rows = [
        {
            "reason": "ungrounded",
            "fired": is_open and grounded is not None and grounded < GROUNDED_MIN,
        },
        {
            "reason": "news_against",
            "fired": is_open and news is not None and news > NEWS_AGAINST_MAX,
        },
        {
            "reason": "generic_and_contested",
            "fired": is_open
            and catalyst is not None
            and news is not None
            and catalyst < CATALYST_MIN
            and news > CONTESTED_NEWS_MIN,
        },
    ]
    reason = next((row["reason"] for row in rows if row["fired"]), None)
    return RuleResult(would_veto=reason is not None, reason=reason, rows=rows)


# --- the state ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionState:
    side: str  # "long" | "short"
    quantity: float
    summary: str | None  # the held-position review's text, when the Bot reads that lane
    opened_at: datetime | None
    open_rationale: str | None  # the opening decision's rationale; None = not recovered


def _research_lane(piece: SourceSlice) -> dict[str, str]:
    dated = piece.available_at or piece.as_of
    return {"text": piece.text, "as_of": dated.isoformat()}


def _research(ctx: DecisionContext) -> dict[str, dict[str, str]]:
    """Every research lane the decide prompt read, by Source name, each in its own
    rendered text. Empty lanes are dropped: an empty field can read as a signal."""
    pieces = [*ctx.sources, *([ctx.sentiment] if ctx.sentiment else []), *ctx.signal_lanes]
    research: dict[str, dict[str, str]] = {}
    for piece in pieces:
        if piece.source in _NOT_RESEARCH or piece.source in research or not piece.text.strip():
            continue
        research[piece.source] = _research_lane(piece)
    if BRIEF not in research and ctx.brief is not None and ctx.brief.entries:
        research[BRIEF] = {"text": "\n".join(ctx.brief.entries)}
    return research


def _market(ctx: DecisionContext) -> dict[str, Any]:
    market: dict[str, Any] = {
        "price": ctx.price,
        "indicators": {interval: ind.as_dict() for interval, ind in ctx.indicators.items()},
    }
    closes = {
        interval: [bar.close for bar in bars[-CLOSES_PER_INTERVAL:]]
        for interval, bars in ctx.bars.items()
        if bars
    }
    if closes:
        market["last_10_closes"] = closes
    if ctx.triggers:
        market["triggers"] = [f"{t.type}: {t.detail}" for t in ctx.triggers]
    if ctx.fred_series:
        market["fred"] = {
            p.series_id: {"value": p.value, "date": p.obs_date.isoformat()} for p in ctx.fred_series
        }
    return market


def _context(ctx: DecisionContext) -> dict[str, list[str]]:
    """The advisory lanes the decide prompt shows besides research: the operator's Macro
    View, the Bot's Lessons and the analyst desk's reads, as the prompt renders them.
    The desk's reads are the analysts' opinions, not news, so they stay out of ``research``
    (and ``news_against``); rationales cite them, so ``grounded`` must see them."""
    context: dict[str, list[str]] = {}
    if ctx.macro_view is not None and ctx.macro_view.entries:
        context["macro_view"] = list(ctx.macro_view.entries)
    if ctx.lessons is not None and ctx.lessons.detail:
        context["lessons"] = [str(entry) for entry in ctx.lessons.detail]
    if ctx.analysts:
        context["analyst_desk"] = list(ctx.analysts)
    return context


def build_state(
    ctx: DecisionContext, decision: Decision, position: PositionState | None
) -> dict[str, Any]:
    """The named JSON state Jev judges: ``symbol``, ``draft``, ``market``, and
    ``research`` / ``context`` / ``position`` when there is any. No notional: sizing is code's."""
    intent = decision.intent
    state: dict[str, Any] = {
        "symbol": {"ticker": ctx.symbol.ticker, "asset_class": str(ctx.symbol.asset_class)},
        "draft": {
            "action": str(decision.action),
            "side": str(intent.side) if intent is not None else None,
            "rationale": decision.rationale,
            "conviction": decision.conviction,
        },
        "market": _market(ctx),
    }
    research = _research(ctx)
    if research:
        state["research"] = research
    context = _context(ctx)
    if context:
        state["context"] = context
    if position is not None:
        held: dict[str, Any] = {"side": position.side, "quantity": position.quantity}
        if position.summary:
            held["summary"] = position.summary
        held["opened_at"] = position.opened_at.isoformat() if position.opened_at else None
        held["open_rationale"] = position.open_rationale
        state["position"] = held
    return state
