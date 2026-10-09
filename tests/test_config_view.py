"""Read the running configuration (#94): every setting with its value, default and where
the value came from, on every door (registry → API → MCP → CLI). Credentials say only
whether they are set, and a URL's password never leaves the process.
"""

from __future__ import annotations

import json

SECRET = "sekret-value-123"


def test_the_running_config_is_readable_on_every_door_without_secrets(clean_db: None, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.ops.config as config_module
    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings
    from trader.ops.config_reference import collect_fields
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    settings = Settings(
        _env_file=None,
        auth_disabled=True,
        api_key=SECRET,
        loop_symbols="BTC/USD",
    )
    monkeypatch.setattr(config_module, "_settings", settings)
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))

    async def via_mcp() -> dict:
        async with Client(build_mcp(build_registry(None))) as mcp_client:
            result = await mcp_client.call_tool("get_config", {})
        assert not result.is_error, result.content
        return result.structured_content

    with TestClient(create_app(settings)) as tc:
        via_api = tc.get("/config").json()
    printed = CliRunner().invoke(cli.app, ["config", "show"])
    assert printed.exit_code == 0, printed.output
    assert json.loads(printed.output) == via_api == asyncio.run(via_mcp())

    by_env = {f["env_name"]: f for f in via_api["fields"]}
    assert len(by_env) == len(collect_fields())  # every documented setting, no more

    api_key = by_env["TRADER_API_KEY"]
    assert (api_key["value"], api_key["source"], api_key["category"]) == (
        "set",
        "configured",
        "credential",
    )
    assert by_env["TRADER_CAPTURE_API_KEY"]["value"] == "not set"
    assert by_env["TRADER_CAPTURE_API_KEY"]["source"] == "default"

    # The default store URL carries a password too: value and default are both masked.
    db_url = by_env["TRADER_DATABASE_URL"]
    assert db_url["value"].startswith("postgresql://trader:***@")
    assert db_url["default"].startswith("postgresql://trader:***@")
    # A URL without a password is shown as is.
    assert by_env["TRADER_NEMOTRON_BASE_URL"]["default"] == "http://localhost:8009/v1"

    loop = by_env["TRADER_LOOP_SYMBOLS"]
    assert (loop["value"], loop["source"]) == ("BTC/USD", "configured")
    assert loop["section"] == "Decide loop" and loop["description"]

    cooldown = by_env["TRADER_RISK_STOP_COOLDOWN_SECONDS"]
    assert (cooldown["value"], cooldown["default"], cooldown["source"]) == (
        86_400,
        86_400,
        "default",
    )

    for text in (json.dumps(via_api), printed.output):
        assert SECRET not in text and "trader:trader@" not in text
