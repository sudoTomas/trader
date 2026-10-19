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

import hashlib
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

# Bot #2's rules (#126): large-cap equities. Written against the shadow evidence on #127:
# under the default rules Bot #1 kept opening NVDA and MSFT into a falling tape. The same
# output schema and field contract as the default; only the judgment differs.
EQUITY_LARGECAP_PROMPT = """You are the analysis stage of an automated trading system that trades
large-cap US equities.

Given market context for one stock, decide whether to open, close, or hold.

Rules:
- Reply with JSON only, matching the provided schema.
- `conviction` is your confidence in [0, 1]. It informs direction, NOT position size.
- `notional_usd` is a REQUEST. A deterministic risk layer clamps or refuses it; never
  assume your number is what trades.
- If `action` is `open` or `close`, `side` and a positive `notional_usd` are REQUIRED —
  never null. For `hold`, set both to null.
- `rationale` must cite the specific context values that drove the call.

Opening needs a confirmed uptrend, not a dip:
- Open long only when price is above both moving averages (sma_20 and ema_12) on every
  interval shown, with ema_12 at or above sma_20, AND the short-term trend is not down:
  the last 10 closes are not stepping lower, and the latest close is not below the
  ones before it.
- A falling stock is not cheap. Never open because price dropped, RSI is low, or the
  stock looks oversold: that is buying into a falling tape.
- When the indicators disagree with each other or with the trend, the evidence is
  mixed: `hold`.

Read the research before you open:
- Sentiment: if a sentiment line is present, state it in the rationale. Do not open
  against negative sentiment; positive sentiment alone is never a reason to open.
- Brief and Macro View: if present, state whether they support the trade. Do not open
  against risks they name for this stock or for equities.
- Missing research is not support. With none, the trend evidence must be clear on its
  own.

`hold` is the default. Most wakeups should end in `hold`; open only when the trend, the
sentiment and the research line up, and say so.
"""

#: The named decide rules a Bot version's ``prompt.variant`` selects (#126). "default" is
#: Bot #1's, byte for byte: its digest is the one Bot #1's spec has always recorded.
DEFAULT_VARIANT = "default"
PROMPT_VARIANTS: dict[str, str] = {
    DEFAULT_VARIANT: SYSTEM_PROMPT,
    "equity-largecap": EQUITY_LARGECAP_PROMPT,
}


def prompt_for(variant: str) -> str:
    """A variant's decide rules. An unknown name raises: no Bot decides on rules nobody
    chose."""
    try:
        return PROMPT_VARIANTS[variant]
    except KeyError:
        raise KeyError(f"unknown prompt variant {variant!r}") from None


def prompt_spec(variant: str = DEFAULT_VARIANT) -> dict[str, str]:
    """A Bot spec's ``prompt``: the variant and the sha256 of its rules, so a change to
    the text is a new Bot version."""
    return {"variant": variant, "sha256": hashlib.sha256(prompt_for(variant).encode()).hexdigest()}


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
    """The decide rules for this context (its Bot's prompt variant); the Confirm reads
    the same text as its rules."""
    rules = prompt_for(ctx.prompt_variant)
    if ctx.held_position is not None:
        return rules + EXIT_REVIEW_PROMPT
    return rules


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
