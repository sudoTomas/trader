"""Usage metering: real token counts, priced per model, rolled into ``usage_daily``.

v1 recorded ``None`` locally so budget gates always read $0 — here every completion the
registry serves is recorded with the provider-reported counts, and unknown models fail
loud rather than silently metering at zero.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from trader.llm.base import Completion
from trader.store.repos import UsageRepo

log = structlog.get_logger(__name__)

#: (provider, model) -> (USD per 1M prompt tokens, USD per 1M completion tokens).
#: Nemotron runs on our own Spark: $0 by definition. xAI prices as of 2026-08.
PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("nemotron", "nemotron-3-super"): (0.0, 0.0),
    ("xai", "grok-4"): (3.00, 15.00),
    ("xai", "grok-4-mini"): (0.30, 0.50),
}


def price_per_million(provider: str, model: str) -> tuple[float, float]:
    try:
        return PRICES[(provider, model)]
    except KeyError:
        raise KeyError(
            f"no price entry for ({provider}, {model}) — add it to trader.llm.usage.PRICES"
        ) from None


class UsageMeter:
    """Rolls completions into ``usage_daily`` per (day, role, provider, model)."""

    def __init__(self, repo: UsageRepo) -> None:
        self._repo = repo

    async def record(self, role: str, completion: Completion) -> None:
        await self._repo.record(
            day=datetime.now(tz=UTC).date(),
            role=role,
            provider=completion.provider,
            model=completion.model,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            cost_usd=completion.cost_usd,
        )
        log.info(
            "llm_usage",
            role=role,
            provider=completion.provider,
            model=completion.model,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            cost_usd=completion.cost_usd,
        )
