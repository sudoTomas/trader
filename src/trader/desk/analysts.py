"""Analyst fan-out — focused parallel reads over the already-built context.

Each analyst is one ``analyst``-role completion with a focused system prompt over the
SAME ``DecisionContext`` the decider sees — no new I/O in the hot path (binding tools
into v1's decide call cost 120 s timeouts; see
docs/lessons.md#indicators-over-mcp-returned-null). The roster is dynamic:
``technical`` always runs (bars are always in context); ``research`` runs only when a
research lane (Macro View / Candidate Brief) is actually present — an analyst with no
material would emit confident noise.

Contradictions between analysts are information for the decider, not something to
resolve — the reads land side by side as labeled lanes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from trader.llm.base import LLMError
from trader.loop.context import DecisionContext

log = structlog.get_logger(__name__)

ROLE = "analyst"

TECHNICAL_PROMPT = """You are the TECHNICAL analyst on a trading desk.

Read ONLY the price action in the context: bars, indicators (RSI/SMA/EMA/realized vol),
and the open position. Ignore any research, macro, or sentiment sections entirely.

Take a side. If the balance of evidence leans at all — trend vs mean-reversion, RSI
position, price vs SMA/EMA — say `bullish` or `bearish` with a confidence that matches
how slight the lean is. `neutral` is reserved for indicators that genuinely conflict,
and its note must name the conflict. A reflexive neutral is a wasted seat at the desk.

Reply with JSON matching the provided schema: your stance, a confidence in [0, 1], and
one short note citing the specific values that drove it. No filler."""

RESEARCH_PROMPT = """You are the RESEARCH analyst on a trading desk.

Read ONLY the research sections in the context (Operator Macro View, Candidate Brief).
Ignore raw price action — the technical analyst owns that.

Reply with JSON matching the provided schema: your stance, a confidence in [0, 1], and
one short note citing the specific research claims that drove it. If the research is
contradictory, say so — the contradiction is the information. No filler."""


class AnalystOutput(BaseModel):
    """One analyst's contract; its JSON schema constrains the model."""

    stance: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    note: str = Field(min_length=1, max_length=400)


ANALYST_SCHEMA: dict[str, Any] = AnalystOutput.model_json_schema()


def render_reads(reads: list[dict[str, Any]]) -> list[str]:
    """Prompt lane lines from analyst reads; failed lanes drop (advisory never blocks)."""
    return [
        f"[{r['lane']}] {r['stance']} ({r['confidence']:.2f}): {r['note']}"
        for r in reads
        if r["ok"]
    ]


class AnalystPool:
    """Run the analyst roster concurrently; one dict per lane, ok or not."""

    def __init__(self, llm: Any) -> None:  # LLMRegistry-shaped
        self._llm = llm

    def roster(self, ctx: DecisionContext) -> list[tuple[str, str]]:
        lanes = [("technical", TECHNICAL_PROMPT)]
        if ctx.macro_view is not None or ctx.brief is not None:
            lanes.append(("research", RESEARCH_PROMPT))
        return lanes

    async def analyze(self, ctx: DecisionContext) -> list[dict[str, Any]]:
        lanes = self.roster(ctx)
        prompt = ctx.to_prompt()
        reads = await asyncio.gather(*(self._one(lane, system, prompt) for lane, system in lanes))
        return list(reads)

    async def _one(self, lane: str, system: str, prompt: str) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        try:
            completion = await self._llm.complete(ROLE, messages, schema=ANALYST_SCHEMA)
            output = AnalystOutput.model_validate_json(completion.text)
        except (LLMError, ValidationError, ValueError) as exc:
            # One lane down is a degraded desk, never a blocked decide.
            log.warning("analyst_lane_failed", lane=lane, error=str(exc)[:300])
            return {"lane": lane, "ok": False, "error": str(exc)[:300]}
        return {
            "lane": lane,
            "ok": True,
            "stance": output.stance,
            "confidence": output.confidence,
            "note": output.note,
        }
