"""Grok client against ``api.x.ai`` with OAuth-or-API-key credential resolution.

Consumers get an ordinary ``OpenAIChatClient``; per-request auth resolves in this order:

1. SuperGrok OAuth access token (when a credential manager exists and isn't quarantined)
2. ``XAI_API_KEY`` (first-class fallback — also covers OAuth quarantine at runtime)

Endpoint note (spike, docs/research/xai-api.md): we target ``/chat/completions``;
whether the OAuth bearer is honored there vs only on ``/v1/responses`` is verified
live by `trader xai probe` (``llm/xai_probe.py``), which runs inside the service on
the same ``ManagedBearerAuth`` client this module builds (``xai_http_client``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx

from trader.llm.base import LLMError
from trader.llm.openai_compat import OpenAIChatClient
from trader.llm.xai_auth import XaiAuthError, XaiCredentialManager, build_manager
from trader.ops.config import Settings

PROVIDER = "xai"

#: Request extension a caller may set to pin the credential for ONE request
#: (``"oauth"`` | ``"api_key"``; absent or ``"auto"`` keeps OAuth-then-key). Diagnostics
#: only (``llm/xai_probe.py``) — the roles never set it, so their path is unchanged.
CREDENTIAL_EXTENSION = "xai_credential"
#: Request extension the auth flow writes back: which credential signed the request.
CREDENTIAL_USED_EXTENSION = "xai_credential_used"
CREDENTIAL_AUTO = "auto"
CREDENTIAL_OAUTH = "oauth"
CREDENTIAL_API_KEY = "api_key"


class ManagedBearerAuth(httpx.Auth):
    """Resolves the bearer per request: OAuth first, API-key fallback, else fail as an
    LLMError so the role registry falls through to the next provider."""

    def __init__(self, manager: XaiCredentialManager | None, fallback_key: str) -> None:
        self._manager = manager
        self._fallback_key = fallback_key

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        wanted = request.extensions.get(CREDENTIAL_EXTENSION, CREDENTIAL_AUTO)
        token: str | None = None
        used: str | None = None
        if wanted != CREDENTIAL_API_KEY and self._manager is not None:
            try:
                token = await self._manager.get_access_token()
                used = CREDENTIAL_OAUTH
            except XaiAuthError as exc:
                if wanted == CREDENTIAL_OAUTH:
                    raise LLMError(f"xai: OAuth credential unavailable: {exc}") from exc
                token = None  # absent/quarantined/transient → try the plain key
        if token is None:
            if wanted == CREDENTIAL_OAUTH:
                raise LLMError("xai: OAuth credential unavailable (TRADER_XAI_TOKEN_KEY unset)")
            if not self._fallback_key:
                raise LLMError("xai: no usable credential (OAuth unavailable, no XAI_API_KEY)")
            token = self._fallback_key
            used = CREDENTIAL_API_KEY
        request.headers["Authorization"] = f"Bearer {token}"
        request.extensions[CREDENTIAL_USED_EXTENSION] = used
        yield request


def xai_http_client(
    settings: Settings, *, manager: XaiCredentialManager | None = None
) -> httpx.AsyncClient | None:
    """The authenticated ``api.x.ai`` client the roles use (OAuth first, ``XAI_API_KEY``
    fallback), or None when this build has no xAI credential path at all.

    Diagnostics (``llm/xai_probe.py``) reuse this so they exercise the exact credential
    path the roles do: no second OAuth client, no token-file access outside the manager.
    A ``manager`` handed in is used as-is (one per process, #63); ``build_manager`` is
    only the fallback for store-less callers.
    """
    manager = manager if manager is not None else build_manager(settings)
    if manager is None and not settings.xai_api_key:
        return None
    return httpx.AsyncClient(
        base_url=settings.xai_base_url,
        auth=ManagedBearerAuth(manager, settings.xai_api_key),
        timeout=120.0,
    )


def xai_client(
    settings: Settings,
    *,
    manager: XaiCredentialManager | None = None,
    client: httpx.AsyncClient | None = None,
) -> OpenAIChatClient | None:
    """A Grok client, or None when this build has no xAI credential path at all."""
    http = client or xai_http_client(settings, manager=manager)
    if http is None:
        return None
    return OpenAIChatClient(
        name=PROVIDER, model=settings.xai_model, base_url=settings.xai_base_url, client=http
    )
