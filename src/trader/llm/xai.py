"""Grok client against ``api.x.ai`` with OAuth-or-API-key credential resolution.

Consumers get an ordinary ``OpenAIChatClient``; per-request auth resolves in this order:

1. SuperGrok OAuth access token (when a credential manager exists and isn't quarantined)
2. ``XAI_API_KEY`` (first-class fallback — also covers OAuth quarantine at runtime)

Endpoint note (spike, docs/research/xai-api.md): we target ``/chat/completions``;
whether the OAuth bearer is honored there vs only on ``/v1/responses`` is verified
live after the first `trader xai login`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx

from trader.llm.base import LLMError
from trader.llm.openai_compat import OpenAIChatClient
from trader.llm.xai_auth import XaiAuthError, XaiCredentialManager, build_manager
from trader.ops.config import Settings

PROVIDER = "xai"


class ManagedBearerAuth(httpx.Auth):
    """Resolves the bearer per request: OAuth first, API-key fallback, else fail as an
    LLMError so the role registry falls through to the next provider."""

    def __init__(self, manager: XaiCredentialManager | None, fallback_key: str) -> None:
        self._manager = manager
        self._fallback_key = fallback_key

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token: str | None = None
        if self._manager is not None:
            try:
                token = await self._manager.get_access_token()
            except XaiAuthError:
                token = None  # absent/quarantined/transient → try the plain key
        if token is None:
            if not self._fallback_key:
                raise LLMError("xai: no usable credential (OAuth unavailable, no XAI_API_KEY)")
            token = self._fallback_key
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def xai_client(
    settings: Settings,
    *,
    manager: XaiCredentialManager | None = None,
    client: httpx.AsyncClient | None = None,
) -> OpenAIChatClient | None:
    """A Grok client, or None when this build has no xAI credential path at all."""
    manager = manager if manager is not None else build_manager(settings)
    if manager is None and not settings.xai_api_key:
        return None
    http = client or httpx.AsyncClient(
        base_url=settings.xai_base_url,
        auth=ManagedBearerAuth(manager, settings.xai_api_key),
        timeout=120.0,
    )
    return OpenAIChatClient(
        name=PROVIDER, model=settings.xai_model, base_url=settings.xai_base_url, client=http
    )
