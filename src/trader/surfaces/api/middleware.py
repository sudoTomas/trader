"""API-key auth. Pure ASGI (not BaseHTTPMiddleware) so the /mcp SSE streams pass through
unbuffered.

Two keys, two scopes (#8). The operator key (``TRADER_API_KEY``) opens every door. The
capture key (``TRADER_CAPTURE_API_KEY``) is for external research agents and opens only
the research-capture routes and the ``/mcp-capture/`` mount; anywhere else it is a 403
that names the scope, so a misconfigured client sees *why* rather than a bare 401.
"""

from __future__ import annotations

import re
import secrets

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Liveness must stay probeable by compose/monitoring without a key.
EXEMPT_PATHS = frozenset({"/health"})

OPERATOR_SCOPE = "operator"
CAPTURE_SCOPE = "capture"

# The whole capture surface on REST: submit / list / retire notes, read the Macro View.
# Method + exact path (a note id is one segment). Nothing here can reach the risk chain,
# the broker, the loop, or a research *compile*; those stay operator-only.
CAPTURE_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/research/notes$")),
    ("GET", re.compile(r"^/research/notes$")),
    ("POST", re.compile(r"^/research/notes/[^/]+/retire$")),
    ("GET", re.compile(r"^/research/view$")),
)
# The capture MCP mount (see app.py): the same four capabilities as tools.
CAPTURE_MCP_PREFIX = "/mcp-capture"


def capture_allowed(method: str, path: str) -> bool:
    """True when a capture-scoped caller may reach ``method path``."""
    if path in EXEMPT_PATHS:
        return True
    if path == CAPTURE_MCP_PREFIX or path.startswith(CAPTURE_MCP_PREFIX + "/"):
        return True
    return any(method == m and pattern.match(path) for m, pattern in CAPTURE_ROUTES)


def _matches(provided: str, configured: str) -> bool:
    """Constant-time equality; an empty configured key never matches anything."""
    return bool(configured) and secrets.compare_digest(provided, configured)


class ApiKeyMiddleware:
    """Rejects any non-exempt HTTP request whose X-API-Key matches neither key, and any
    capture-key request outside the capture surface.

    ``Settings.require_auth()`` already refuses to boot with no operator key and no
    explicit dev opt-out; this enforces the same contract per request. A blank
    ``capture_api_key`` (the default) means no second key exists.
    """

    def __init__(
        self, app: ASGIApp, *, api_key: str, enabled: bool, capture_api_key: str = ""
    ) -> None:
        self._app = app
        self._api_key = api_key
        self._capture_api_key = capture_api_key
        self._enabled = enabled

    def scope_of(self, provided: str) -> str | None:
        """Which scope a presented key grants, or None. Both comparisons always run so the
        answer takes the same time whichever key (or neither) matched."""
        operator = _matches(provided, self._api_key)
        capture = _matches(provided, self._capture_api_key)
        if operator:
            return OPERATOR_SCOPE
        if capture:
            return CAPTURE_SCOPE
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._enabled or scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return
        provided = Headers(scope=scope).get("x-api-key", "")
        granted = self.scope_of(provided)
        if granted is None:
            response = JSONResponse({"detail": "invalid or missing X-API-Key"}, status_code=401)
            await response(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        if granted == CAPTURE_SCOPE and not capture_allowed(method, path):
            response = JSONResponse(
                {
                    "detail": (
                        f"{CAPTURE_SCOPE}-scoped key cannot reach {method} {path}; it may only "
                        "submit/list/retire research notes, read the macro view, and use "
                        f"{CAPTURE_MCP_PREFIX}/"
                    ),
                    "scope": CAPTURE_SCOPE,
                },
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)
