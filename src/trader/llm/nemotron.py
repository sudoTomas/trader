"""Nemotron client — the money-path ``decide`` model, served by a self-hosted vLLM.

INVARIANT (CLAUDE.md): every request MUST send
``chat_template_kwargs: {"enable_thinking": false}`` — omitting it collapses the
self-hosted endpoint to ~0.35 tok/s (see docs/lessons.md#nemotron-thinking-off). The flag is
baked into the client here so no caller can forget it.

No auth on the endpoint — network trust inside the LAN. Shared 4-sequence vLLM
instance; keep research roles off it when another endpoint is configured (they
default here only as the backstop). Selected when ``TRADER_DECIDE_BASE_URL`` is
empty. ``TRADER_NEMOTRON_*`` is the preset, not a second client implementation.
"""

from __future__ import annotations

from typing import Any

import httpx

from trader.llm.openai_compat import OpenAIChatClient
from trader.ops.config import Settings

PROVIDER = "nemotron"


def apply_thinking_guard(extra_body: dict[str, Any]) -> dict[str, Any]:
    """Force ``enable_thinking: false``. Other extra-body keys are kept.

    The self-hosted endpoint collapses to ~0.35 tok/s without this flag
    (see docs/lessons.md#nemotron-thinking-off).
    The Nemotron preset always sends it. A generic decide endpoint aimed at
    ``TRADER_NEMOTRON_BASE_URL`` does too, so the flag cannot be omitted or
    flipped off for that host.
    """
    merged = dict(extra_body)
    current = merged.get("chat_template_kwargs")
    kwargs = dict(current) if isinstance(current, dict) else {}
    kwargs["enable_thinking"] = False
    merged["chat_template_kwargs"] = kwargs
    return merged


#: Baked into every preset request — see module docstring.
CHAT_TEMPLATE_KWARGS = apply_thinking_guard({})


def nemotron_client(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> OpenAIChatClient:
    return OpenAIChatClient(
        name=PROVIDER,
        model=settings.nemotron_model,
        base_url=settings.nemotron_base_url,
        extra_body=CHAT_TEMPLATE_KWARGS,
        client=client,
    )
