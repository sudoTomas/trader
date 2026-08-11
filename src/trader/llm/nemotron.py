"""Nemotron client — the money-path ``decide`` model, served by vLLM on the Spark.

INVARIANT (CLAUDE.md): every request MUST send
``chat_template_kwargs: {"enable_thinking": false}`` — omitting it collapses the Spark
endpoint to ~0.35 tok/s (v1:EAI-128). The flag is baked into the client here so no
caller can forget it.

No auth on the endpoint — network trust inside the Spark. Shared 4-sequence vLLM
instance; keep research roles off it (they live on Grok partly for this reason).
"""

from __future__ import annotations

import httpx

from trader.llm.openai_compat import OpenAIChatClient
from trader.ops.config import Settings

PROVIDER = "nemotron"

#: Baked into every request — see module docstring.
CHAT_TEMPLATE_KWARGS = {"chat_template_kwargs": {"enable_thinking": False}}


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
