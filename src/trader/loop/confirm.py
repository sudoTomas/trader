"""Confirm — Kev's judgment of an open or close Draft, journal-only in this stage.

Order on a non-hold Draft: context, analyst desk, Draft (decide), **Confirm**, critic,
RiskGuard, broker. The Confirm always judges the raw Draft, so its column replays the same
whether or not the critic was up. A hold Draft is never sent.

In this stage the Confirm has no power at all: ``confirm()`` returns a journal record and
nothing else, the runner never reads it back, and the verdict is always ``journal_only``.
The margin that will later let it turn a Draft into a hold is chosen from this column by
replay, then switched on in its own change.

Failure is a skip, never a blocked wakeup: when no answer comes back (unreachable, slow,
HTTP error, malformed answers) the record says ``skipped`` and why, by status text only,
and the Draft proceeds unchanged.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from trader.core.types import Decision
from trader.desk.critic import draft_under_review
from trader.llm.base import LLMError
from trader.loop.context import DecisionContext
from trader.loop.decider import system_prompt

log = structlog.get_logger(__name__)

ROLE = "confirm"
JOURNAL_ONLY = "journal_only"

QUESTIONS: dict[str, Any] = {
    "action": {
        "type": "choice",
        "instructions": (
            "You are the decision step of a risk-guarded trading agent. Read the decide "
            "prompt in `prompt` and the agent's operating rules in `rules`, then choose what "
            "the agent should do now for the symbol in the prompt. Deterministic risk code "
            "sizes and vets any trade afterwards; your job is only the intent."
        ),
        "criteria": {
            "hold": (
                "Do nothing now: no defensible edge, the setup is unclear, or a held "
                "position's thesis still stands."
            ),
            "open": (
                "Open a new position: there is a clear, defensible edge and no position is "
                "currently held."
            ),
            "close": (
                "Close the held position: its thesis has failed or the edge is gone. Only "
                "valid when a position is held."
            ),
        },
    },
    "open_side": {
        "type": "choice",
        "instructions": (
            "If the agent opened a position on the symbol in `prompt` right now, which side has "
            "the better case?"
        ),
        "criteria": {
            "buy": "Long: the case for the price rising is stronger.",
            "sell": "Short: the case for the price falling is stronger.",
        },
    },
    "close_side": {
        "type": "choice",
        "instructions": (
            "Suppose the agent closes the position described in `prompt`. Which order side "
            "closes it?"
        ),
        "criteria": {
            "sell": "Sell: the held position is long, or no position is described.",
            "buy": "Buy: the held position is short.",
        },
    },
}


def draft_of(decision: Decision) -> dict[str, Any]:
    assert decision.intent is not None, "a hold Draft is never confirmed"
    return {"action": decision.action.value, "side": decision.intent.side.value}


def skipped(decision: Decision, reason: str) -> dict[str, Any]:
    """The record when no Confirm was produced: the Draft and why, nothing else."""
    return {"draft": draft_of(decision), "skipped": reason[:200]}


def _probabilities(answer: Any) -> dict[str, float]:
    probs = {str(k): float(v) for k, v in answer["probabilities"].items()}
    if not probs:
        raise ValueError("no probabilities")
    return probs


def _top(probs: dict[str, float]) -> str:
    return max(probs, key=probs.__getitem__)


class Confirmer:
    """One Jev call per open or close Draft. Returns a journal record, never a Decision."""

    def __init__(self, llm: Any) -> None:  # LLMRegistry-shaped
        self._llm = llm

    async def confirm(self, ctx: DecisionContext, decision: Decision) -> dict[str, Any]:
        draft = draft_of(decision)
        side_question = f"{draft['action']}_side"
        questions = {"action": QUESTIONS["action"], side_question: QUESTIONS[side_question]}
        messages = [
            {"role": "system", "content": system_prompt(ctx)},
            {"role": "user", "content": draft_under_review(ctx, decision)},
        ]
        started = time.perf_counter()
        try:
            completion = await self._llm.complete(ROLE, messages, schema=questions)
        except LLMError as exc:
            # The provider's own error ("jev: HTTP 500"), without the provider prefix.
            cause = exc.__cause__ if isinstance(exc.__cause__, LLMError) else exc
            reason = str(cause).split(": ", 1)[-1]
            log.warning("confirm_skipped", symbol=decision.symbol, reason=reason)
            return skipped(decision, reason)
        latency_ms = round((time.perf_counter() - started) * 1000)
        try:
            answers = json.loads(completion.text)
            action = _probabilities(answers["action"])
            side = _probabilities(answers[side_question])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            reason = f"malformed answers: {type(exc).__name__}"
            log.warning("confirm_skipped", symbol=decision.symbol, reason=reason)
            return skipped(decision, reason)

        prefers = _top(action)
        if prefers == draft["action"] and _top(side) != draft["side"]:
            prefers = "other_side"
        return {
            "draft": draft,
            "action": action,
            "side": side,
            "p_same_action": action.get(draft["action"], 0.0),
            "prefers": prefers,
            "margin": None,
            "verdict": JOURNAL_ONLY,
            "model": completion.model,
            "latency_ms": latency_ms,
            "skipped": None,
        }
