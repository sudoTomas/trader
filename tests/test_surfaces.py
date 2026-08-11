"""EAI-168: one capability, three doors — auth gating, /mcp mount, and audit parity
across registry, API, MCP, and CLI. Runs against the real compose Postgres.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client
from typer.testing import CliRunner

from trader.ops.config import Settings
from trader.store.audit import audit
from trader.store.db import Database
from trader.surfaces.api.app import create_app
from trader.surfaces.mcp.server import build_mcp
from trader.surfaces.services import build_registry

DEV_SETTINGS = Settings(auth_disabled=True)
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


def test_health_reports_db_reachability(clean_db: None):
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        body = tc.get("/health").json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


def test_api_key_gating(clean_db: None):
    app = create_app(Settings(api_key="sekret", auth_disabled=False))
    with TestClient(app) as tc:
        assert tc.get("/health").status_code == 200  # liveness stays probeable
        assert tc.get("/audit").status_code == 401
        assert tc.get("/audit", headers={"x-api-key": "wrong"}).status_code == 401
        assert tc.get("/audit", headers={"x-api-key": "sekret"}).status_code == 200
        # /mcp sits behind the same key.
        assert tc.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
        ok = tc.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": "sekret"})
        assert ok.status_code == 200


def test_mcp_mount_answers_initialize(clean_db: None):
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        resp = tc.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert resp.status_code == 200
    assert "serverInfo" in resp.text


def test_audit_parity_across_all_surfaces(clean_db: None, monkeypatch: pytest.MonkeyPatch):
    """The same audit row must come back byte-identical through every door."""

    async def seed_and_query_direct() -> tuple[list, list]:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            await audit(
                db,
                actor="test",
                category="config",
                action="parity_probe",
                subject="BTC/USD",
                payload={"n": 1},
            )
            registry = build_registry(db)
            via_registry = await registry.get("audit").handler(limit=5)
            async with Client(build_mcp(registry)) as mcp_client:
                tools = await mcp_client.list_tools()
                assert {"get_health", "get_audit"} <= {t.name for t in tools.tools}
                call = await mcp_client.call_tool("get_audit", {"limit": 5})
            via_mcp = call.structured_content["result"]
            return via_registry, via_mcp
        finally:
            await db.close()

    via_registry, via_mcp = asyncio.run(seed_and_query_direct())

    app = create_app(DEV_SETTINGS)
    with TestClient(app) as tc:
        via_api = tc.get("/audit", params={"limit": 5}).json()

    import trader.surfaces.cli.main as cli

    # A fresh app per CLI call: a closed psycopg pool cannot reopen, so the app the earlier
    # TestClient block shut down is spent.
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(DEV_SETTINGS)))
    result = CliRunner().invoke(cli.app, ["audit", "--limit", "5"])
    assert result.exit_code == 0, result.output
    via_cli = json.loads(result.output)

    assert len(via_registry) == 1
    assert via_registry[0]["action"] == "parity_probe"
    assert via_registry == via_api == via_mcp == via_cli


def test_risk_status_surfaced_on_api(clean_db: None):
    """Risk state is operator-visible on every door (registry → API → MCP → CLI)."""
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        body = tc.get("/risk/status").json()
        assert body["trading_paused"] is False
        assert body["live_enabled"] is False  # paper-only by default
        assert body["limits"]["max_position_frac"] > 0
        assert tc.get("/risk/approvals").json() == []
