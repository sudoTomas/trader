"""The Desk — analyst fan-out before the decide, critic review after it.

    ContextBuilder → Desk.enrich (analyst lanes) → Decider → Desk.review (critic)
                   → RiskGuard (unchanged, still the sole chokepoint)

Application of the critic verdict happens HERE, deterministically:

- ``approve`` / ``pass_through`` — the draft stands.
- ``shrink`` — ``min(draft, critic)`` notional: a "shrink" above the draft clamps to the
  draft (journaled ``clamped``), so enlargement is impossible even if the model tries.
- ``veto`` — the decision becomes a hold; the draft's conviction/rationale survive for
  the journal, the veto reason lives in the desk verdicts.

Holds never reach the critic (nothing to brake, and it keeps a mostly-hold loop's Grok
spend near zero). The critic Role's Spend cap (#108, default
``TRADER_DESK_CRITIC_DAILY_USD``) bounds the rest in the shared registry: at the cap its
paid models sit out the day and a free one in its Chain reviews; with none left, the
review is a journaled pass-through (critic unavailable).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import structlog

from trader.core.types import Action, Decision
from trader.desk.analysts import AnalystPool, render_reads
from trader.desk.critic import Critic
from trader.loop.context import DecisionContext

log = structlog.get_logger(__name__)


class Desk:
    def __init__(
        self,
        llm: Any,  # LLMRegistry-shaped
    ) -> None:
        self._analysts = AnalystPool(llm)
        self._critic = Critic(llm)

    async def enrich(self, ctx: DecisionContext) -> tuple[DecisionContext, dict[str, Any]]:
        """Fan out the analysts and attach their reads as prompt lanes."""
        reads = await self._analysts.analyze(ctx)
        return replace(ctx, analysts=render_reads(reads)), {"analysts": reads}

    async def review(
        self, ctx: DecisionContext, decision: Decision, desk_info: dict[str, Any]
    ) -> tuple[Decision, dict[str, Any]]:
        """Critic pass over the draft; returns the final decision + full desk verdicts."""
        before = {
            "action_before": decision.action.value,
            "notional_before": decision.intent.notional if decision.intent else None,
        }
        if decision.intent is None:
            verdict: dict[str, Any] = {"verdict": "skipped", "reason": "hold needs no critique"}
        else:
            verdict = await self._critic.review(ctx, decision)

        final = self._apply(decision, verdict)
        info = {
            **desk_info,
            "critic": verdict,
            "applied": {
                **before,
                "action_after": final.action.value,
                "notional_after": final.intent.notional if final.intent else None,
            },
        }
        return final, info

    # -- internals -----------------------------------------------------------

    def _apply(self, decision: Decision, verdict: dict[str, Any]) -> Decision:
        if verdict["verdict"] == "veto":
            return Decision(
                symbol=decision.symbol,
                action=Action.HOLD,
                conviction=decision.conviction,
                rationale=decision.rationale,
                intent=None,
            )
        if verdict["verdict"] == "shrink":
            assert decision.intent is not None
            asked = verdict.get("notional_usd")
            if not asked or decision.intent.notional is None:
                verdict["verdict"] = "pass_through"
                verdict["reason"] = f"unusable shrink target: {asked} — {verdict['reason']}"[:300]
                return decision
            if asked >= decision.intent.notional:
                # A "shrink" that grows clamps to the draft: enlargement is impossible.
                verdict["clamped"] = True
                return decision
            return replace(decision, intent=replace(decision.intent, notional=asked))
        return decision  # approve | pass_through | skipped
