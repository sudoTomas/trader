"""Capture-scoped API key (#8): a second key that can ONLY submit / list / retire research
notes and read the Macro View, on REST and on its own ``/mcp-capture/`` mount. The
operator key keeps every door; the capture key is refused everywhere else with a 403 that
names its scope. Runs against the real compose Postgres.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from trader.ops.config import Settings
from trader.surfaces.api.app import create_app
from trader.surfaces.api.middleware import CAPTURE_SCOPE
from trader.surfaces.mcp.server import CAPTURE_CAPABILITIES, build_mcp
from trader.surfaces.services import build_registry

OPERATOR = "operator-sekret"
CAPTURE = "capture-sekret"
SETTINGS = Settings(api_key=OPERATOR, capture_api_key=CAPTURE, auth_disabled=False)

MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

NOTE = {"title": "ETF flows", "body": "second negative week", "symbols": ["ETH/USD"]}

# Everything the capture key may reach, with a request that exercises the door.
CAPTURE_ALLOWED = [
    ("POST", "/research/notes", {"json": NOTE}),
    ("GET", "/research/notes", {}),
    ("POST", "/research/notes/1/retire", {}),
    ("GET", "/research/view", {}),
]
# A sample of what it must never reach: the operator doors the issue names, plus the
# read-only journal/ledger and the neighbouring research commands.
CAPTURE_DENIED = [
    ("POST", "/risk/pause", {}),
    ("POST", "/risk/resume", {}),
    ("POST", "/broker/flatten-all", {}),
    ("POST", "/loop/wake", {}),
    ("POST", "/risk/approvals/1/resolve", {"params": {"approved": "true"}}),
    ("POST", "/research/view/compile", {}),
    ("POST", "/research/mirror/export", {}),
    ("POST", "/research/briefs/compile", {"params": {"symbol": "SPY"}}),
    ("POST", "/research/sentiment/fetch", {"params": {"symbol": "SPY"}}),
    ("GET", "/audit", {}),
    ("GET", "/decisions", {}),
    ("GET", "/positions", {}),
    ("GET", "/orders", {}),
    ("GET", "/risk/status", {}),
    ("GET", "/loop/status", {}),
    ("GET", "/research/briefs", {}),
    ("GET", "/research/sentiment", {}),
    ("GET", "/usage", {}),
    ("DELETE", "/research/notes", {}),  # wrong verb on an allowed path
    ("GET", "/research/notes/1/retire", {}),  # wrong verb on an allowed path
]


def _call(tc: TestClient, method: str, path: str, key: str | None, extra: dict):
    headers = {} if key is None else {"x-api-key": key}
    return tc.request(method, path, headers=headers, **extra)


# --- REST -------------------------------------------------------------------


def test_capture_key_reaches_only_the_capture_doors(clean_db: None):
    with TestClient(create_app(SETTINGS)) as tc:
        for method, path, extra in CAPTURE_ALLOWED:
            resp = _call(tc, method, path, CAPTURE, extra)
            assert resp.status_code == 200, (method, path, resp.text)
        for method, path, extra in CAPTURE_DENIED:
            resp = _call(tc, method, path, CAPTURE, extra)
            assert resp.status_code == 403, (method, path, resp.text)
            body = resp.json()
            assert body["scope"] == CAPTURE_SCOPE
            assert path in body["detail"] and method in body["detail"]


def test_capture_key_note_round_trip(clean_db: None):
    """The four doors compose: submit, list, retire, then the view is readable."""
    with TestClient(create_app(SETTINGS)) as tc:
        created = tc.post("/research/notes", json=NOTE, headers={"x-api-key": CAPTURE}).json()
        listed = tc.get("/research/notes", headers={"x-api-key": CAPTURE}).json()
        assert [n["id"] for n in listed] == [created["id"]]
        retired = tc.post(f"/research/notes/{created['id']}/retire", headers={"x-api-key": CAPTURE})
        assert retired.status_code == 200
        assert tc.get("/research/notes", headers={"x-api-key": CAPTURE}).json() == []
        assert tc.get("/research/view", headers={"x-api-key": CAPTURE}).status_code == 200


def test_operator_key_keeps_every_door(clean_db: None):
    with TestClient(create_app(SETTINGS)) as tc:
        for method, path, extra in CAPTURE_ALLOWED:
            resp = _call(tc, method, path, OPERATOR, extra)
            assert resp.status_code == 200, (method, path, resp.text)
        # Operator doors answer (200 or a domain-level refusal), never an auth error.
        assert tc.post("/risk/pause", headers={"x-api-key": OPERATOR}).status_code == 200
        assert tc.post("/risk/resume", headers={"x-api-key": OPERATOR}).status_code == 200
        assert tc.get("/audit", headers={"x-api-key": OPERATOR}).status_code == 200
        assert tc.post("/loop/wake", headers={"x-api-key": OPERATOR}).status_code == 200


def test_missing_or_wrong_key_is_401_on_capture_doors(clean_db: None):
    with TestClient(create_app(SETTINGS)) as tc:
        assert tc.get("/research/notes").status_code == 401
        assert tc.get("/research/notes", headers={"x-api-key": "nope"}).status_code == 401
        assert tc.get("/research/notes", headers={"x-api-key": ""}).status_code == 401
        assert tc.get("/health").status_code == 200  # liveness stays exempt


def test_empty_capture_key_never_matches(clean_db: None):
    """No capture key configured (the default) means no second key exists at all."""
    app = create_app(Settings(api_key=OPERATOR, capture_api_key="", auth_disabled=False))
    with TestClient(app) as tc:
        assert tc.get("/research/notes", headers={"x-api-key": ""}).status_code == 401
        assert tc.get("/research/notes").status_code == 401
        assert tc.get("/research/notes", headers={"x-api-key": OPERATOR}).status_code == 200
        # /mcp-capture/ exists but nothing but the operator key opens it.
        assert tc.post("/mcp-capture/", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
        assert (
            tc.post(
                "/mcp-capture/", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": ""}
            ).status_code
            == 401
        )


def test_empty_operator_key_never_matches_even_with_capture_key(clean_db: None):
    """A blank operator key must not become a wildcard when only a capture key is set."""
    # require_auth() refuses that configuration at boot, so probe the middleware itself:
    # wrap a dev app (its own gate off) in a gate with a blank operator key.
    from trader.surfaces.api.middleware import ApiKeyMiddleware

    inner = create_app(Settings(auth_disabled=True))
    gated = ApiKeyMiddleware(inner, api_key="", capture_api_key=CAPTURE, enabled=True)
    with TestClient(gated) as tc:
        assert tc.get("/audit", headers={"x-api-key": ""}).status_code == 401
        assert tc.get("/research/notes", headers={"x-api-key": ""}).status_code == 401
        assert tc.get("/research/notes", headers={"x-api-key": CAPTURE}).status_code == 200
        assert tc.get("/audit", headers={"x-api-key": CAPTURE}).status_code == 403


# --- MCP ---------------------------------------------------------------------


def _mcp_tools(tc: TestClient, mount: str, key: str) -> set[str]:
    """Drive the streamable-HTTP handshake by hand and return the tool names listed."""
    headers = {**MCP_HEADERS, "x-api-key": key}
    init = tc.post(mount, json=INITIALIZE, headers=headers)
    assert init.status_code == 200, init.text
    session = {"mcp-session-id": init.headers["mcp-session-id"]}
    tc.post(mount, json=INITIALIZED, headers={**headers, **session})
    listed = tc.post(mount, json=LIST_TOOLS, headers={**headers, **session})
    assert listed.status_code == 200, listed.text
    payloads = [
        json.loads(line[len("data:") :])
        for line in listed.text.splitlines()
        if line.startswith("data:")
    ]
    if not payloads:  # plain JSON response mode
        payloads = [listed.json()]
    (result,) = [p["result"] for p in payloads if "result" in p]
    return {t["name"] for t in result["tools"]}


def test_capture_key_opens_mcp_capture_and_not_mcp(clean_db: None):
    with TestClient(create_app(SETTINGS)) as tc:
        denied = tc.post("/mcp/", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": CAPTURE})
        assert denied.status_code == 403
        assert denied.json()["scope"] == CAPTURE_SCOPE
        assert tc.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
        assert tc.post("/mcp-capture/", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
        ok = tc.post(
            "/mcp-capture/", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": CAPTURE}
        )
        assert ok.status_code == 200
        assert "serverInfo" in ok.text


def test_mcp_capture_mount_lists_only_the_four_tools(clean_db: None):
    expected = {"submit_research_note", "get_research_notes", "retire_note", "get_macro_view"}
    with TestClient(create_app(SETTINGS)) as tc:
        assert _mcp_tools(tc, "/mcp-capture/", CAPTURE) == expected
        assert _mcp_tools(tc, "/mcp-capture/", OPERATOR) == expected  # operator works too
        full = _mcp_tools(tc, "/mcp/", OPERATOR)
        assert expected < full
        assert {"pause", "flatten_all", "wake_trader", "resolve_approval"} <= full


def test_build_mcp_only_allowlist_is_exact_and_fails_closed():
    registry = build_registry(None)

    async def names(only: frozenset[str] | None) -> set[str]:
        async with Client(build_mcp(registry, only=only)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    everything = asyncio.run(names(None))
    capture = asyncio.run(names(CAPTURE_CAPABILITIES))
    assert capture == {
        "submit_research_note",
        "get_research_notes",
        "retire_note",
        "get_macro_view",
    }
    assert capture < everything
    # An allowlist naming an unknown capability is a typo that would silently widen or
    # narrow the surface: refuse to build.
    with pytest.raises(ValueError, match="unknown capability"):
        build_mcp(registry, only=frozenset({"submit_research_note", "place_order"}))
