"""Per-role model routing: ``{role -> [client, fallback, ...]}`` resolved from config.

Roles (docs/architecture.md §LLM layer): ``decide`` runs on the local Nemotron; ``brief``
/ ``compiler`` / ``sentiment`` run on Grok. Every completion served here is metered into
``usage_daily`` under its role — there is no unmetered path.
"""

from __future__ import annotations

from typing import Any

import structlog

from trader.llm.base import Completion, LLMClient, LLMError
from trader.llm.nemotron import nemotron_client
from trader.llm.usage import UsageMeter
from trader.llm.xai import xai_client
from trader.ops.config import Settings

log = structlog.get_logger(__name__)

ROLES = ("decide", "brief", "compiler", "sentiment")


class LLMRegistry:
    """Ordered fallback chain per role, with usage metering on every completion."""

    def __init__(self, routes: dict[str, list[LLMClient]], meter: UsageMeter | None) -> None:
        self._routes = routes
        self._meter = meter

    def chain_for(self, role: str) -> list[LLMClient]:
        chain = self._routes.get(role, [])
        if not chain:
            raise LLMError(f"no LLM provider configured for role '{role}'")
        return chain

    async def complete(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> Completion:
        errors: list[str] = []
        for client in self.chain_for(role):
            try:
                completion = await client.complete(
                    messages, schema=schema, max_tokens=max_tokens, temperature=temperature
                )
            except LLMError as exc:
                errors.append(str(exc))
                log.warning(
                    "llm_provider_failed",
                    role=role,
                    provider=client.name,
                    model=client.model,
                    error=str(exc),
                )
                continue
            if self._meter is not None:
                await self._meter.record(role, completion)
            return completion
        raise LLMError(f"all providers failed for role '{role}': {errors}")


def build_llm_registry(settings: Settings, meter: UsageMeter | None = None) -> LLMRegistry:
    """Wire the chains this build can serve from configured credentials.

    Grok roles are present when any xAI credential path exists — SuperGrok OAuth
    (device-code login) or the first-class ``XAI_API_KEY`` fallback; consumers can't
    tell which one is serving.
    """
    nemotron = nemotron_client(settings)
    routes: dict[str, list[LLMClient]] = {"decide": [nemotron]}
    grok = xai_client(settings)
    if grok is not None:
        # Nemotron backstops research roles if xAI is quarantined (architecture §Desk).
        for role in ("brief", "compiler", "sentiment"):
            routes[role] = [grok, nemotron]
    return LLMRegistry(routes, meter)
