"""LLM contract: one ``complete()`` shape for every provider, real usage on every reply.

No global-endpoint assumption (v1's core LLM-layer debt): callers name a *role*, the
registry resolves the provider chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """A provider could not answer. The registry catches these to fall through."""


@dataclass(frozen=True)
class Usage:
    """Real token counts as reported by the provider — never inferred locally."""

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    cost_usd: float
    provider: str
    model: str


@runtime_checkable
class LLMClient(Protocol):
    """One provider+model endpoint."""

    name: str  # provider name, e.g. "nemotron" | "xai"
    model: str

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> Completion:
        """One chat completion. ``schema`` switches the provider into
        JSON-schema-constrained output for schema-validated decides."""
        ...


def require_all_keys(schema: dict[str, Any]) -> dict[str, Any]:
    """Wire-schema hardening: force guided decoding to EMIT every key (nullable values
    stay nullable — holds write nulls). Under long prompts models omit optional keys
    they should have filled (EAI-190 gate: 5 opens without side/notional, invalid
    twice each); making omission ungrammatical fixes generation, while pydantic
    validation stays lenient for stubs and non-guided providers."""
    return {**schema, "required": sorted(schema["properties"])}
