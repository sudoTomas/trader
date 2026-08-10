"""API-key auth. Pure ASGI (not BaseHTTPMiddleware) so the /mcp SSE streams pass through
unbuffered.
"""

from __future__ import annotations

import secrets

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Liveness must stay probeable by compose/monitoring without a key.
EXEMPT_PATHS = frozenset({"/health"})


class ApiKeyMiddleware:
    """Rejects any non-exempt HTTP request whose X-API-Key doesn't match TRADER_API_KEY.

    ``Settings.require_auth()`` already refuses to boot with no key and no explicit dev
    opt-out; this enforces the same contract per request.
    """

    def __init__(self, app: ASGIApp, *, api_key: str, enabled: bool) -> None:
        self._app = app
        self._api_key = api_key
        self._enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._enabled or scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return
        provided = Headers(scope=scope).get("x-api-key", "")
        if not (self._api_key and secrets.compare_digest(provided, self._api_key)):
            response = JSONResponse({"detail": "invalid or missing X-API-Key"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)
