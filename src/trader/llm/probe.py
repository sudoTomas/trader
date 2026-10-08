"""The Probe (#107): one call in a Role's real contract before a model may serve it.

Each Role's contract is the schema its caller sends and the validation its caller runs;
the Probe sends that schema on a synthetic context and runs that validation, so a model
that cannot produce a valid decide (Kev as decider: 150 holds out of 150) is refused at
save time instead of discovered in the journal. Probe calls go straight to the client,
never through the registry, so they are not metered into ``usage_daily``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from trader.llm.base import LLMError

SYNTHETIC_CONTEXT = """Symbol: SPY (us_equity). Price 500.00. RSI(14) 50.1, SMA(20) 499.80,
EMA(20) 500.05, realized vol 0.8%. No position held. This is a connectivity probe:
answer within the schema, a hold is fine."""


@dataclass(frozen=True)
class Contract:
    system: str
    schema: dict[str, Any]
    validate: Callable[[str], object]
    max_tokens: int = 512


def _contracts() -> dict[str, Contract]:
    # Imported here: these modules sit above the LLM layer and import the registry.
    from trader.desk.analysts import ANALYST_SCHEMA, TECHNICAL_PROMPT, AnalystOutput
    from trader.desk.critic import CRITIC_SCHEMA, CriticOutput
    from trader.desk.critic import SYSTEM_PROMPT as CRITIC_PROMPT
    from trader.loop.confirm import QUESTIONS
    from trader.loop.decider import DECISION_SCHEMA, SYSTEM_PROMPT, DecisionOutput
    from trader.research.briefs import BRIEF_SCHEMA, BriefOutput
    from trader.research.briefs import SYSTEM_PROMPT as BRIEF_PROMPT
    from trader.research.compiler import COMPILE_SCHEMA, CompiledOutput
    from trader.research.compiler import SYSTEM_PROMPT as COMPILE_PROMPT

    def confirm_answers(text: str) -> None:
        answers = json.loads(text)
        for name in ("action", "open_side"):
            if not isinstance(answers[name]["probabilities"], dict):
                raise ValueError(f"{name} has no probabilities")

    return {
        "decide": Contract(
            SYSTEM_PROMPT,
            DECISION_SCHEMA,
            lambda t: DecisionOutput.model_validate_json(t).to_decision("SPY"),
        ),
        "analyst": Contract(TECHNICAL_PROMPT, ANALYST_SCHEMA, AnalystOutput.model_validate_json),
        "critic": Contract(CRITIC_PROMPT, CRITIC_SCHEMA, CriticOutput.model_validate_json),
        "brief": Contract(BRIEF_PROMPT, BRIEF_SCHEMA, BriefOutput.model_validate_json),
        "compiler": Contract(COMPILE_PROMPT, COMPILE_SCHEMA, CompiledOutput.model_validate_json),
        "confirm": Contract(
            SYSTEM_PROMPT,
            {"action": QUESTIONS["action"], "open_side": QUESTIONS["open_side"]},
            confirm_answers,
        ),
    }


async def probe(role: str, client: Any) -> dict[str, Any]:
    """One call in ``role``'s contract. ``ok`` with the latency, or the status-text reason."""
    started = time.perf_counter()
    try:
        if role == "sentiment":
            # One x_search call; an answer at all proves the model takes the tool.
            await client.search(SYNTHETIC_CONTEXT, from_date="2026-01-01", to_date="2026-01-02")
        else:
            contract = _contracts()[role]
            completion = await client.complete(
                [
                    {"role": "system", "content": contract.system},
                    {"role": "user", "content": SYNTHETIC_CONTEXT},
                ],
                schema=contract.schema,
                max_tokens=contract.max_tokens,
            )
            contract.validate(completion.text)
    except (LLMError, httpx.HTTPError) as exc:
        # Status text only: an HTTP error's message carries the URL and can carry more.
        cause = exc.__cause__ if isinstance(exc, LLMError) else exc
        if isinstance(cause, httpx.HTTPStatusError):
            error = f"HTTP {cause.response.status_code}"
        elif isinstance(cause, httpx.HTTPError):
            error = f"unreachable ({type(cause).__name__})"
        else:
            error = str(exc)
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        # The reply arrived but broke the contract: name the failure, not the reply.
        error = f"reply breaks the {role} contract ({type(exc).__name__})"
    else:
        error = None
    latency_ms = round((time.perf_counter() - started) * 1000)
    return {"ok": error is None, "latency_ms": latency_ms, "error": error}
