"""Confirm — Jev's judgments of an open or close Draft, and whether an open is vetoed.

Order on a non-hold Draft: context, analyst desk, Draft (decide), **Confirm**, critic,
RiskGuard, broker. The Confirm always judges the raw Draft, so its column replays the same
whether or not the critic was up. A hold Draft is never sent.

Jev answers the atomic Noul judgments of ``loop/confirm_questions.py`` (p(true) each) about a
named JSON state; the rule there decides whether an open would be vetoed. ``confirm()``
returns the journal record and nothing else: ``vetoed`` (would_veto, the switch on, and an
open) is the signal the loop applies by turning the Draft into a hold. A close is judged and
recorded, never vetoed.

Failure is a skip, never a blocked wakeup and never a veto: when no answer comes back
(unreachable, slow, HTTP error, malformed answers) the record says ``skipped`` and why, by
status text only, and the Draft proceeds unchanged.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import structlog

from trader.core.types import Action, Decision
from trader.llm.base import LLMError
from trader.llm.registry import NoRoute
from trader.loop.confirm_questions import (
    QUESTIONS_VERSION,
    PositionState,
    build_state,
    evaluate,
    questions_for,
)
from trader.loop.context import DecisionContext

log = structlog.get_logger(__name__)

ROLE = "confirm"


def draft_of(decision: Decision) -> dict[str, Any]:
    assert decision.intent is not None, "a hold Draft is never confirmed"
    return {"action": decision.action.value, "side": decision.intent.side.value}


def skipped(decision: Decision, reason: str) -> dict[str, Any]:
    """The record when no Confirm was produced: the Draft and why. Never a veto."""
    return {"draft": draft_of(decision), "skipped": reason[:200], "vetoed": False}


def veto_hold(decision: Decision, record: dict[str, Any] | None) -> Decision:
    """The Draft the rest of the chain sees: a hold when the Confirm vetoed an open, else
    the Draft unchanged. The Draft itself stays in ``record["draft"]``."""
    if record is None or not record.get("vetoed") or decision.action is not Action.OPEN:
        return decision
    return Decision(
        symbol=decision.symbol,
        action=Action.HOLD,
        conviction=decision.conviction,
        rationale=f"confirm veto: {record.get('veto_reason') or 'unspecified'}",
    )


def _answers(text: str, asked: dict[str, Any]) -> dict[str, float]:
    """Each asked judgment's p(true), or ValueError/KeyError/TypeError when any is missing,
    not a number, or outside [0, 1]."""
    raw = json.loads(text)
    answers: dict[str, float] = {}
    for qid in asked:
        p = raw[qid]["noul"]
        if isinstance(p, bool) or not isinstance(p, int | float):
            raise TypeError(f"{qid} is not a number")
        if math.isnan(p) or not 0.0 <= p <= 1.0:
            raise ValueError(f"{qid} is outside [0, 1]")
        answers[qid] = float(p)
    return answers


class Confirmer:
    """One Jev call per open or close Draft. Returns a journal record, never a Decision;
    None when nothing serves the confirm Role (the column stays NULL)."""

    def __init__(self, llm: Any, *, veto_enabled: bool = True) -> None:  # LLMRegistry-shaped
        self._llm = llm
        self._veto_enabled = veto_enabled

    async def confirm(
        self,
        ctx: DecisionContext,
        decision: Decision,
        position: PositionState | None = None,
    ) -> dict[str, Any] | None:
        draft = draft_of(decision)
        if ctx.blind is not None:
            # The state names the symbol, its prices and dates; a blind step never sends it.
            return skipped(decision, "blind")
        state = build_state(ctx, decision, position)
        questions = questions_for(
            draft["action"],
            has_research="research" in state,
            has_open_rationale=position is not None
            and isinstance(position.open_rationale, str)
            and bool(position.open_rationale.strip()),
        )
        started = time.perf_counter()
        try:
            completion = await self._llm.complete(ROLE, [], schema=questions, state=state)
        except NoRoute:
            return None  # no Confirm configured, or switched off in the active Profile
        except LLMError as exc:
            # The provider's own error ("jev: HTTP 500"), without the provider prefix.
            cause = exc.__cause__ if isinstance(exc.__cause__, LLMError) else exc
            reason = str(cause).split(": ", 1)[-1]
            log.warning("confirm_skipped", symbol=decision.symbol, reason=reason)
            return skipped(decision, reason)
        latency_ms = round((time.perf_counter() - started) * 1000)
        try:
            answers = _answers(completion.text, questions)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            reason = f"malformed answers: {type(exc).__name__}"
            log.warning("confirm_skipped", symbol=decision.symbol, reason=reason)
            return skipped(decision, reason)

        is_open = draft["action"] == "open"
        rule = evaluate(answers, action=draft["action"])
        vetoed = rule.would_veto and self._veto_enabled and is_open
        if not is_open:
            verdict = "recorded"
        else:
            verdict = "veto" if vetoed else "pass"
        if rule.would_veto:
            log.info(
                "confirm_would_veto",
                symbol=decision.symbol,
                reason=rule.reason,
                vetoed=vetoed,
            )
        return {
            "draft": draft,
            "answers": answers,
            "rule": rule.rows,
            "would_veto": rule.would_veto,
            "veto_reason": rule.reason,
            "vetoed": vetoed,
            "verdict": verdict,
            "questions_version": QUESTIONS_VERSION,
            "model": completion.model,
            "latency_ms": latency_ms,
            "skipped": None,
        }
