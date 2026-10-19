"""One capability, three doors — auth gating, /mcp mount, and audit parity across
registry, API, MCP, and CLI (see docs/lessons.md#operator-doors-on-all-surfaces).
Runs against the real compose Postgres.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client
from typer.testing import CliRunner

from trader.loop.bots import BOT_ONE, BotDesk, bot_one_spec
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


def test_health_reports_the_pyproject_version(clean_db: None):
    """pyproject.toml is the single version; services.VERSION reads it, never restates it."""
    import ast
    import tomllib
    from pathlib import Path

    import trader.surfaces.services as services

    root = Path(services.__file__).resolve().parents[3]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        body = tc.get("/health").json()
    assert body["version"] == pyproject
    assert services.VERSION == pyproject

    tree = ast.parse(Path(services.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Name)
                    and target.id == "VERSION"
                    and isinstance(node.value, ast.Constant)
                ), "services.VERSION must not be a second literal copy of the version"


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
            # Seed Bot #1 as boot will (#123), so the app's boot audits nothing new.
            await BotDesk(db).seed(BOT_ONE, bot_one_spec(DEV_SETTINGS))
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

    assert [r["action"] for r in via_registry] == ["parity_probe", "seed_bot_version"]
    assert via_registry == via_api == via_mcp == via_cli


def test_usage_parity_across_all_surfaces(clean_db: None, monkeypatch: pytest.MonkeyPatch):
    """usage_daily is readable, identically, on registry, API, MCP, and CLI."""
    from datetime import UTC, datetime, timedelta

    from trader.store.repos import UsageRepo

    today = datetime.now(tz=UTC).date()
    yesterday = today - timedelta(days=1)

    async def seed_and_query_direct() -> tuple[dict, dict]:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            repo = UsageRepo(db)
            await repo.record(
                day=today,
                role="sentiment",
                provider="xai",
                model="grok-4",
                prompt_tokens=100,
                completion_tokens=40,
                cost_usd=1.10,
            )
            await repo.record(
                day=yesterday,
                role="compiler",
                provider="xai",
                model="grok-4",
                prompt_tokens=10,
                completion_tokens=4,
                cost_usd=0.05,
            )
            registry = build_registry(db)
            via_registry = await registry.get("usage").handler(days=2)
            async with Client(build_mcp(registry)) as mcp_client:
                tools = await mcp_client.list_tools()
                assert "get_usage" in {t.name for t in tools.tools}
                call = await mcp_client.call_tool("get_usage", {"days": 2})
            payload = call.structured_content
            # A bare list is wrapped as {"result": ...}; a dict is the object itself.
            via_mcp = payload.get("result", payload) if "rows" not in payload else payload
            return via_registry, via_mcp
        finally:
            await db.close()

    via_registry, via_mcp = asyncio.run(seed_and_query_direct())

    app = create_app(DEV_SETTINGS)
    with TestClient(app) as tc:
        via_api = tc.get("/usage", params={"days": 2}).json()
        sentiment = tc.get("/usage", params={"days": 2, "role": "sentiment"}).json()
        one_day = tc.get("/usage", params={"day": yesterday.isoformat()}).json()

    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(DEV_SETTINGS)))
    result = CliRunner().invoke(cli.app, ["usage", "--days", "2"])
    assert result.exit_code == 0, result.output
    via_cli = json.loads(result.output)

    assert via_registry == via_api == via_mcp == via_cli
    assert [r["role"] for r in via_registry["rows"]] == ["sentiment", "compiler"]
    assert via_registry["rows"][0]["day"] == today.isoformat()
    assert via_registry["rows"][0]["cost_usd"] == 1.1
    assert via_registry["totals"] == {
        "requests": 2,
        "prompt_tokens": 110,
        "completion_tokens": 44,
        "cost_usd": 1.15,
    }
    assert [r["role"] for r in sentiment["rows"]] == ["sentiment"]
    assert sentiment["totals"]["cost_usd"] == 1.1
    assert {r["role"] for r in one_day["rows"]} == {"compiler"}
    assert one_day["totals"]["cost_usd"] == 0.05


def test_risk_status_surfaced_on_api(clean_db: None):
    """Risk state is operator-visible on every door (registry → API → MCP → CLI)."""
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        body = tc.get("/risk/status").json()
        assert body["trading_paused"] is False
        assert body["live_enabled"] is False  # paper-only by default
        assert body["limits"]["max_position_frac"] > 0
        assert tc.get("/risk/approvals").json() == []
