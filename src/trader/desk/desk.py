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
spend near zero). A per-day critic budget caps the rest: at the cap, reviews degrade to
journaled pass-throughs until tomorrow.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import structlog

from trader.core.types import Action, Decision
from trader.desk.analysts import AnalystPool, render_reads
from trader.desk.critic import Critic
from trader.loop.context import DecisionContext

log = structlog.get_logger(__name__)

DEFAULT_CRITIC_DAILY_USD = 1.0


class Desk:
    def __init__(
        self,
        llm: Any,  # LLMRegistry-shaped
        *,
        usage: Any | None = None,  # store.repos.UsageRepo; absent = no budget check
        critic_daily_usd: float = DEFAULT_CRITIC_DAILY_USD,
    ) -> None:
        self._analysts = AnalystPool(llm)
        self._critic = Critic(llm)
        self._usage = usage
        self._critic_daily_usd = critic_daily_usd

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
        elif await self._budget_spent():
            verdict = {"verdict": "pass_through", "reason": "daily critic budget reached"}
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

    async def _budget_spent(self) -> bool:
        if self._usage is None:
            return False
        rows = await self._usage.for_day(datetime.now(tz=UTC).date())
        spent = sum(float(r["cost_usd"]) for r in rows if r["role"] == "critic")
        if spent >= self._critic_daily_usd:
            log.warning("critic_budget_reached", spent=spent, cap=self._critic_daily_usd)
            return True
        return False
