"""Usage metering: real token counts, priced per model, rolled into ``usage_daily``.

v1 recorded ``None`` locally so budget gates always read $0 — here every completion the
registry serves is recorded with the provider-reported counts. ``PRICES`` is the
default table. A configurable override replaces a hit; an unknown model uses the
configured default (0 unless the caller passes an endpoint price) and is logged
once. A new model id must never raise mid-loop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog

from trader.llm.base import Completion
from trader.store.repos import UsageRepo

log = structlog.get_logger(__name__)

#: (provider, model) -> (USD per 1M prompt tokens, USD per 1M completion tokens).
#: Nemotron runs on our own Spark: $0 by definition. xAI short-context rates from
#: docs.x.ai/developers/pricing (read 2026-09-17). Long-context (≥200k prompt) is
#: 2×; we do not take that path. ``grok-4`` stays because it is still the live default.
PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("nemotron", "nemotron-3-super"): (0.0, 0.0),
    ("xai", "grok-4"): (3.00, 15.00),
    ("xai", "grok-4-mini"): (0.30, 0.50),
    ("xai", "grok-4.6"): (2.00, 6.00),
    ("xai", "grok-4.5"): (2.00, 6.00),
    ("xai", "grok-4.3"): (1.25, 2.50),
    ("xai", "grok-4.20-0309-reasoning"): (1.25, 2.50),
    ("xai", "grok-4.20-0309-non-reasoning"): (1.25, 2.50),
    ("xai", "grok-4.20-multi-agent-0309"): (1.25, 2.50),
}

#: x_search tool fees (docs.x.ai/developers/pricing). Until this instant: $5 / 1k
#: calls. From it: $5 / 1k posts fetched + $10 / 1k profiles; parent/quoted posts count.
X_SEARCH_CUTOVER = datetime(2026, 9, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
X_SEARCH_PER_1K_CALLS_USD = 5.0
X_SEARCH_PER_1K_POSTS_USD = 5.0
X_SEARCH_PER_1K_PROFILES_USD = 10.0


#: (provider, model) pairs already warned about. One log per process, not per call.
_unknown_price_logged: set[tuple[str, str]] = set()


def _log_unknown_price_once(provider: str, model: str, price: tuple[float, float]) -> None:
    key = (provider, model)
    if key in _unknown_price_logged:
        return
    _unknown_price_logged.add(key)
    log.warning(
        "llm_price_unknown",
        provider=provider,
        model=model,
        price_in=price[0],
        price_out=price[1],
    )


def price_per_million(
    provider: str,
    model: str,
    *,
    default: tuple[float, float] = (0.0, 0.0),
    override: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """USD per 1M (prompt, completion) tokens.

    Table hit, unless ``override`` is set (a non-zero ``TRADER_DECIDE_PRICE_*`` /
    ``TRADER_RESEARCH_PRICE_*``). A miss logs once and returns ``override`` if
    given, else ``default`` — never raises.
    """
    key = (provider, model)
    if override is not None:
        if key not in PRICES:
            _log_unknown_price_once(provider, model, override)
        return override
    try:
        return PRICES[key]
    except KeyError:
        _log_unknown_price_once(provider, model, default)
        return default


def x_search_tool_cost_usd(
    *,
    calls: int = 1,
    posts: int = 0,
    profiles: int = 0,
    when: datetime | None = None,
) -> float:
    """USD for one x_search invocation. ``when`` must be tz-aware (defaults to now)."""
    when = when or datetime.now(tz=UTC)
    if when.tzinfo is None:
        raise ValueError("when must be timezone-aware")
    if when < X_SEARCH_CUTOVER:
        return calls * X_SEARCH_PER_1K_CALLS_USD / 1000
    return (posts * X_SEARCH_PER_1K_POSTS_USD + profiles * X_SEARCH_PER_1K_PROFILES_USD) / 1000


def x_search_units_from_citations(citations: list[str]) -> tuple[int, int]:
    """Count billed x_search units from citation URLs.

    Citation-count is a lower bound: parent/quoted posts that the tool fetched but
    did not cite still bill, and we cannot see them on the responses payload.
    """
    posts = 0
    profiles = 0
    for url in citations:
        lowered = url.lower()
        if "/status/" in lowered:
            posts += 1
        elif "x.com/" in lowered or "twitter.com/" in lowered:
            profiles += 1
    return posts, profiles


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
