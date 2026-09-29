"""Cross-model critic — a second opinion between decide and RiskGuard.

The ``critic`` role routes Grok-first so a different model reviews Nemotron's draft
(same-model critique via the Nemotron backstop is weaker but keeps the desk functional
when xAI is quarantined). The verdict vocabulary is the whole power ceiling:

    approve | shrink(notional) | veto(reason)

There is no field for side, symbol, action, or a larger size — the critic is
*structurally* incapable of enlarging or redirecting a trade (asserted by test), and the
deterministic RiskGuard chain still runs after it either way. Failure is always
fail-open to the status quo: an unavailable or incoherent critic is a journaled
``pass_through``, never a blocked decide — the desk is an extra brake, not a dependency.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from trader.core.types import Decision
from trader.llm.base import LLMError, require_all_keys
from trader.loop.context import DecisionContext

log = structlog.get_logger(__name__)

ROLE = "critic"

SYSTEM_PROMPT = """You are the risk-skeptic CRITIC on a trading desk, reviewing a draft \
decision made by another model.

Your ONLY powers, by construction:
- approve: the draft stands as proposed.
- shrink: the draft trades smaller — give the reduced notional_usd. You cannot enlarge.
- veto: the draft does not trade — give the reason.

Rules:
- Judge whether the rationale is actually supported by the context. A confident story \
the data does not back deserves a veto.
- Contradictions between analysts are information the decider already weighed — do NOT \
veto merely because analysts disagree.
- Doing nothing costs nothing; a marginal trade shrinks, a bad one is vetoed.
- Reply with JSON only, matching the provided schema. `reason` must cite the specific \
context or rationale weakness that drove your verdict."""


class CriticOutput(BaseModel):
    """The critic's contract — the schema IS the power ceiling."""

    verdict: Literal["approve", "shrink", "veto"]
    notional_usd: float | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1, max_length=1000)


# Wire schema: a shrink whose notional was omitted would be the decider's omitted-key
# failure all over again — every key must be emitted (null allowed;
# see docs/lessons.md#guided-decoding-omitted-keys).
CRITIC_SCHEMA: dict[str, Any] = require_all_keys(CriticOutput.model_json_schema())


class Critic:
    """One completion per review; every failure degrades to a journaled pass-through."""

    def __init__(self, llm: Any) -> None:  # LLMRegistry-shaped
        self._llm = llm

    async def review(self, ctx: DecisionContext, decision: Decision) -> dict[str, Any]:
        assert decision.intent is not None, "only intents are reviewed; holds skip the critic"
        draft = {
            "action": decision.action.value,
            "side": decision.intent.side.value,
            "notional_usd": decision.intent.notional,
            "conviction": decision.conviction,
            "rationale": decision.rationale,
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"{ctx.to_prompt()}\n\nDraft decision under review:\n"
                f"{json.dumps(draft, indent=2)}",
            },
        ]
        try:
            completion = await self._llm.complete(ROLE, messages, schema=CRITIC_SCHEMA)
            output = CriticOutput.model_validate_json(completion.text)
        except LLMError as exc:
            log.warning("critic_unavailable", error=str(exc)[:300])
            return {"verdict": "pass_through", "reason": f"critic unavailable: {exc}"[:300]}
        except (ValidationError, ValueError) as exc:
            log.warning("critic_output_invalid", error=str(exc)[:300])
            return {"verdict": "pass_through", "reason": f"invalid critic output: {exc}"[:300]}
        return {
            "verdict": output.verdict,
            "notional_usd": output.notional_usd,
            "reason": output.reason,
            "reviewed_by": f"{completion.provider}:{completion.model}",
        }
