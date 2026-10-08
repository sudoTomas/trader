"""Decider — one Nemotron completion per wakeup, schema-validated, one retry.

Contracts:

- **Schema-validated output.** The model is given a JSON schema and its reply is parsed
  through pydantic. A validation failure gets exactly one retry, with the actual
  validation errors fed back so the model can correct itself; a second failure is a
  journaled error, not a crash and not a trade.
- **Transient vs crash classification**: infra blips (endpoint down, timeout, refusal)
  are ``transient`` so the runner backs off without arming a crash loop, while a genuine
  bug propagates. v1 crashed the trader on transient errors
  (see docs/lessons.md#transient-vs-crash-backoff).
- **The decider never sizes anything that matters.** ``notional_usd`` is a *request*;
  RiskGuard clamps it against config limits and measured volatility.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from trader.core.types import Action, Decision, OrderIntent, Side
from trader.llm.base import Completion, LLMError, require_all_keys
from trader.llm.registry import LLMRegistry
from trader.loop.context import DecisionContext

log = structlog.get_logger(__name__)

ROLE = "decide"

SYSTEM_PROMPT = """You are the analysis stage of an automated trading system.

Given market context for one symbol, decide whether to open, close, or hold.

Rules:
- Reply with JSON only, matching the provided schema.
- `conviction` is your confidence in [0, 1]. It informs direction, NOT position size.
- `notional_usd` is a REQUEST. A deterministic risk layer clamps or refuses it; never
  assume your number is what trades.
- Prefer `hold` when the evidence is weak. Doing nothing is a valid, common answer.
- If `action` is `open` or `close`, `side` and a positive `notional_usd` are REQUIRED —
  never null. For `hold`, set both to null.
- `rationale` must cite the specific context values that drove the call.
"""

# Appended only when the context carries a held-position block (the exit-review lane,
# #34): the same flag that renders the block gates this text, so the dark path sends the
# system prompt byte-for-byte unchanged. Exits are `close` on the wire: a smaller
# `notional_usd` is a partial exit (RiskGuard resizes it, the executor caps it at held qty).
EXIT_REVIEW_PROMPT = """
Exit review (a position is held — see "Held position review" in the context):
- `close` exits the held position: `side` opposite to the position, `notional_usd` at or
  above the position's value closes it all; a smaller `notional_usd` reduces it (a partial
  exit). Use it when the original thesis no longer holds, when the position has moved
  against you beyond the tolerance you stated at entry, or when sentiment and price now
  disagree with the reason you are in.
- `hold` keeps the position and must justify staying in — restate the thesis and why it
  survives the current context. "Nothing changed" is not a justification when the P&L or
  sentiment in the review says otherwise.
"""


class DecisionOutput(BaseModel):
    """The decider's contract. Its JSON schema is what the model is constrained to."""

    action: Literal["hold", "open", "close"]
    conviction: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=2000)
    side: Literal["buy", "sell"] | None = None
    # ``ge=0``, not ``gt=0``: models naturally answer 0 for a hold, and rejecting that
    # cost a full extra inference round-trip on the MOST common decision. The positive
    # constraint is enforced below, where it actually matters (and again in OrderIntent).
    notional_usd: float | None = Field(default=None, ge=0)

    def to_decision(self, symbol: str) -> Decision:
        action = Action(self.action)
        intent = None
        if action is not Action.HOLD:
            if self.side is None or not self.notional_usd:
                raise ValueError(
                    f"action '{self.action}' requires a side and a positive notional_usd"
                )
            intent = OrderIntent(symbol=symbol, side=Side(self.side), notional=self.notional_usd)
        return Decision(
            symbol=symbol,
            action=action,
            conviction=self.conviction,
            rationale=self.rationale,
            intent=intent,
        )


# Wire schema: every key must be EMITTED. Under the longer analyst-enriched prompt the
# model omitted side/notional on opens, invalid twice each; guided decoding allowed it
# because optional keys were omittable (see docs/lessons.md#guided-decoding-omitted-keys).
DECISION_SCHEMA: dict[str, Any] = require_all_keys(DecisionOutput.model_json_schema())


def system_prompt(ctx: DecisionContext) -> str:
    """The decide rules for this context; the Confirm reads the same text as its rules."""
    if ctx.held_position is not None:
        return SYSTEM_PROMPT + EXIT_REVIEW_PROMPT
    return SYSTEM_PROMPT


class DeciderError(RuntimeError):
    """A wakeup could not produce a decision.

    ``transient`` means infrastructure (retry later, do not arm a crash loop); otherwise
    the model produced unusable output twice.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class DecisionResult:
    decision: Decision
    prompt: str
    raw_output: str
    completion: Completion
    attempts: int


class Decider:
    def __init__(self, registry: LLMRegistry, *, max_tokens: int = 512) -> None:
        self._registry = registry
        self._max_tokens = max_tokens

    async def decide(self, ctx: DecisionContext) -> DecisionResult:
        prompt = ctx.to_prompt()
        messages = [
            {"role": "system", "content": system_prompt(ctx)},
            {"role": "user", "content": prompt},
        ]
        last_error = ""

        for attempt in (1, 2):
            try:
                completion = await self._registry.complete(
                    ROLE, messages, schema=DECISION_SCHEMA, max_tokens=self._max_tokens
                )
            except LLMError as exc:
                # Infra, not logic: the runner should back off, not treat this as a crash.
                raise DeciderError(f"llm unavailable: {exc}", transient=True) from exc

            try:
                output = DecisionOutput.model_validate_json(completion.text)
                decision = output.to_decision(ctx.symbol.ticker)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                log.warning(
                    "decider_output_invalid",
                    attempt=attempt,
                    symbol=ctx.symbol.ticker,
                    error=last_error[:500],
                )
                if attempt == 1:
                    # Feed the actual errors back — the model can usually self-correct.
                    messages = [
                        *messages,
                        {"role": "assistant", "content": completion.text},
                        {
                            "role": "user",
                            "content": (
                                "That reply failed validation:\n"
                                f"{last_error}\n"
                                "Reply again with JSON only, matching the schema exactly."
                            ),
                        },
                    ]
                    continue
                raise DeciderError(
                    f"model output failed validation twice: {last_error}", transient=False
                ) from exc

            return DecisionResult(
                decision=decision,
                prompt=prompt,
                raw_output=completion.text,
                completion=completion,
                attempts=attempt,
            )

        raise DeciderError("unreachable", transient=False)  # pragma: no cover
