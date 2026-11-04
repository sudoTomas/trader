"""The reflect Role (#143) — a short Lesson from one settled decision.

One completion per settled decision: the decision's prompt (trimmed), what it did, and
its Settlement go in; two to four sentences come out. Metered under ``reflect`` like
every Role, so its Spend cap applies. A failure writes nothing and the decision is tried
again on the next pass; the Lesson only ever reaches a decide prompt through the lessons
Source, which a Bot opts into.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError

from trader.llm.base import require_all_keys

log = structlog.get_logger(__name__)

ROLE = "reflect"
#: How much of the journaled decide prompt the reflection reads: the head carries the
#: symbol, price, indicators and lanes; the tail is older history.
PROMPT_BUDGET_CHARS = 4000

SYSTEM_PROMPT = """You are the REFLECT seat on a trading desk. A decision made by the desk \
has settled: you see the context it was made in, what it did, and what the price did over \
the following five trading days, against a benchmark.

Write a lesson of two to four sentences for the next decision on this symbol:
- Name what in the context did or did not predict the outcome.
- Say what to weigh differently next time, or what to keep doing if it was right.
- One outcome is one sample: no rules, no numbers to apply mechanically, no sizing.
Reply with JSON only, matching the provided schema."""


class LessonOutput(BaseModel):
    """The reflect contract; its JSON schema constrains the model."""

    lesson: str = Field(min_length=1, max_length=700)


REFLECT_SCHEMA: dict[str, Any] = require_all_keys(LessonOutput.model_json_schema())


def reflect_prompt(row: Mapping[str, Any]) -> str:
    """The user side: the decision's context, the decision, and its Settlement."""
    decision = row.get("decision") or {}
    made = {
        "action": decision.get("action"),
        "side": (decision.get("intent") or {}).get("side"),
        "conviction": decision.get("conviction"),
        "rationale": decision.get("rationale"),
    }
    view = {1: "long", -1: "short", 0: "none (hold)"}[int(row["direction"])]
    verdict = {True: "right", False: "wrong", None: "not directional"}[row.get("correct")]
    outcome = (
        f"Over the next {row['horizon_days']} sessions the symbol returned"
        f" {row['forward_return']:+.2%}; the benchmark {row['benchmark']}"
        f" {row['benchmark_return']:+.2%}; excess {row['excess_return']:+.2%}."
        f" Directional view: {view}; it was {verdict}."
    )
    context = (row.get("prompt") or "")[:PROMPT_BUDGET_CHARS]
    return (
        f"Context the decision was made in:\n{context}\n\n"
        f"Decision:\n{json.dumps(made, indent=2)}\n\nOutcome:\n{outcome}"
    )


class Reflector:
    """Writes a Lesson; None for a reply off the contract. An ``LLMError`` (the Role is
    down, or capped with no free model) propagates: the pass stops there."""

    def __init__(self, llm: Any) -> None:  # LLMRegistry-shaped
        self._llm = llm

    async def lesson(self, row: Mapping[str, Any]) -> tuple[str, str] | None:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": reflect_prompt(row)},
        ]
        completion = await self._llm.complete(ROLE, messages, schema=REFLECT_SCHEMA)
        try:
            output = LessonOutput.model_validate_json(completion.text)
        except (ValidationError, ValueError) as exc:
            log.warning("reflect_failed", decision_id=row.get("decision_id"), error=str(exc)[:300])
            return None
        return output.lesson.strip(), f"{completion.provider}:{completion.model}"
