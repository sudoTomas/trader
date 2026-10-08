"""The Role Probe (#106): on demand, Probe the active Profile's Chain for one Role and
report each entry's health and latency. It saves nothing and is never on a timer: the desk
Models page calls it from a button. Seam: the service capability through API (with auth),
MCP and CLI. Model servers are mocked at the transport.
"""

from __future__ import annotations

import json

from tests.test_profiles import (
    CAPTURE,
    CLOSED,
    DECIDE_OK,
    OPERATOR,
    SECRETS_KEY,
    ModelServer,
    api_settings,
    with_local,
)
from trader.store.db import Database


async def test_the_probe_reports_each_entry_and_saves_nothing(db: Database):
    from trader.surfaces.services import build_registry

    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK, ("box", "nemotron"): 500})
    providers, profiles = await with_local(db, server)
    await profiles.create("trial")
    chain = [{"provider": "spark", "model": "qwen3:8b"}, {"provider": "box", "model": "nemotron"}]
    await profiles.set_chain("trial", "decide", chain)
    server.replies[("spark", "qwen3:8b")] = 503  # the primary goes down after the save
    server.replies[("box", "nemotron")] = DECIDE_OK
    registry = build_registry(db, providers=providers, profiles=profiles)

    probed = await registry.get("probe_role").handler(role="decide")

    assert (probed["role"], probed["profile"], probed["version"]) == ("decide", "trial", 2)
    assert [(r["provider"], r["model"], r["ok"]) for r in probed["probe"]] == [
        ("spark", "qwen3:8b", False),
        ("box", "nemotron", True),
    ]
    assert probed["probe"][0]["error"] == "HTTP 503"
    assert all(isinstance(r["latency_ms"], int) for r in probed["probe"])
    assert (await profiles.get("trial"))["version"] == 2  # nothing saved


def test_api_probes_a_role_and_refuses_one_on_env(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:
        tc.post(
            "/providers",
            json={"name": "lan", "preset": "vllm", "base_url": CLOSED},
            headers=OPERATOR,
        )
        tc.post("/profiles", json={"name": "trial"}, headers=OPERATOR)
        tc.put("/profiles/trial/roles/confirm", json={"chain": []}, headers=OPERATOR)

        on_env = tc.post("/roles/decide/probe", headers=OPERATOR)
        assert on_env.status_code == 422 and "serves from .env" in on_env.text
        off = tc.post("/roles/confirm/probe", headers=OPERATOR)
        assert off.status_code == 200, off.text
        assert off.json()["probe"] == []  # confirm is off: nothing to Probe
        assert tc.post("/roles/nope/probe", headers=OPERATOR).status_code == 422
        assert tc.post("/roles/decide/probe", headers=CAPTURE).status_code == 403


def test_the_probe_is_identical_through_every_door(clean_db: None, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.llm.profiles import ProfileDesk
    from trader.llm.providers import ProviderDesk
    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    settings = Settings(auth_disabled=True, secrets_key=SECRETS_KEY)
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    runner = CliRunner()
    for args in (["profile", "create", "trial"], ["profile", "set", "trial", "confirm"]):
        assert runner.invoke(cli.app, args).exit_code == 0

    probed = runner.invoke(cli.app, ["profile", "probe", "confirm"])
    assert probed.exit_code == 0, probed.output
    via_cli = json.loads(probed.output)

    async def via_mcp():
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            providers = ProviderDesk(db, secrets_key=SECRETS_KEY)
            registry = build_registry(db, providers=providers, profiles=ProfileDesk(db, providers))
            async with Client(build_mcp(registry)) as mcp_client:
                result = await mcp_client.call_tool("probe_role", {"role": "confirm"})
            return result.structured_content
        finally:
            await db.close()

    assert via_cli == asyncio.run(via_mcp())
    assert via_cli == {"role": "confirm", "profile": "trial", "version": 2, "probe": []}
