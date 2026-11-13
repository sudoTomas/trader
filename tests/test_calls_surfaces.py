"""The calls capabilities on every door: registry, API, MCP, CLI; 409 when calls are off."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from mcp.client import Client
from typer.testing import CliRunner

from trader.ops.config import Settings
from trader.store.db import Database
from trader.surfaces.api.app import create_app
from trader.surfaces.mcp.server import build_mcp
from trader.surfaces.services import build_registry

OFF = "Telegram calls are off or no session is stored"
NAMES = {"channel_posts", "channel_calls", "call_scorecard", "calls_poll", "calls_backfill"}


def boot(**kwargs) -> Settings:
    return Settings(_env_file=None, auth_disabled=True, **kwargs)


def test_surfaces_answer_409_when_calls_off(clean_db: None, monkeypatch) -> None:
    with TestClient(create_app(boot())) as tc:
        for method, path in [
            ("GET", "/calls/scorecard"),
            ("GET", "/calls"),
            ("GET", "/calls/posts"),
            ("POST", "/calls/poll"),
            ("POST", "/calls/backfill"),
        ]:
            resp = tc.request(method, path)
            assert resp.status_code == 409, (path, resp.text)
            assert resp.json() == {"detail": OFF}

    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(boot())))
    result = CliRunner().invoke(cli.app, ["calls", "scorecard"])
    assert result.exit_code == 1
    # TestClient raises its own httpx's HTTPStatusError, which the CLI's handler (for the
    # real httpx) does not catch; either way the 409 is what the CLI met.
    assert "409" in result.output + str(result.exception)


def test_calls_capabilities_are_registered_and_on_mcp(clean_db: None) -> None:
    registry = build_registry(None)
    names = {c.name: c.kind for c in registry.all()}
    assert NAMES <= set(names)
    assert names["calls_poll"] == names["calls_backfill"] == "command"
    assert names["call_scorecard"] == names["channel_calls"] == names["channel_posts"] == "query"

    async def tools_and_refusal() -> tuple[set[str], object]:
        async with Client(build_mcp(registry)) as mcp_client:
            tools = {t.name for t in (await mcp_client.list_tools()).tools}
            refused = await mcp_client.call_tool("get_call_scorecard", {})
        return tools, refused

    tools, refused = asyncio.run(tools_and_refusal())
    assert {"get_call_scorecard", "calls_poll", "calls_backfill"} <= tools
    assert {"get_channel_calls", "get_channel_posts"} <= tools
    assert refused.is_error and OFF in str(refused.content)


class FakeDesk:
    async def scorecard(self) -> dict:
        return {"calls": 0, "gate_pass": False}

    async def calls(self, *, limit: int = 100) -> list[dict]:
        return [{"limit": limit}]

    async def posts(self, *, limit: int = 50) -> list[dict]:
        return [{"limit": limit}]

    async def poll(self) -> dict:
        return {"poll": {"new": 0, "edited": 0}}

    async def backfill(self) -> dict:
        return {"backfill": {"stored": 0}}


def test_the_calls_desk_answers_through_registry_and_mcp(clean_db: None) -> None:
    async def run() -> tuple[dict, list, object]:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            registry = build_registry(db, calls=lambda: FakeDesk())
            card = await registry.get("call_scorecard").handler()
            listed = await registry.get("channel_calls").handler(limit=3)
            async with Client(build_mcp(registry)) as mcp_client:
                polled = await mcp_client.call_tool("calls_poll", {})
            audited = await db.fetch_all(
                "SELECT action FROM audit_events WHERE action = 'calls_poll'"
            )
            assert len(audited) == 1
            return card, listed, polled
        finally:
            await db.close()

    card, listed, polled = asyncio.run(run())
    assert card == {"calls": 0, "gate_pass": False}
    assert listed == [{"limit": 3}]
    assert not polled.is_error
    assert polled.structured_content == {"poll": {"new": 0, "edited": 0}}
