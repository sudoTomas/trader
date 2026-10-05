"""`GET /config` — the effective settings snapshot of the running service, on every door
(registry, API, MCP, CLI). Metadata comes from `trader.ops.config_reference`, the same
derivation `scripts/gen_config_reference.py` renders; values come from the live
`Settings`. Secrets never leave: credentials report only whether they are set, and
passwords embedded in URLs or DSNs are masked.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client
from typer.testing import CliRunner

from trader.ops.config import CREDENTIAL, SECTIONS, Settings
from trader.ops.config_reference import collect_fields, mask_secrets, snapshot
from trader.surfaces.api.app import create_app
from trader.surfaces.api.middleware import CAPTURE_SCOPE
from trader.surfaces.mcp.server import build_mcp
from trader.surfaces.services import build_registry

# Distinctive values so a substring search cannot hit ordinary description text.
OPERATOR = "zq7OPk3y-4f1e9a"
CAPTURE = "zq7CAPk3y-8b2d0c"
XAI = "zq7XAIk3y-6e5f1d"
DB_PASSWORD = "zq7DBpw-3c9a7e"

KEYS = {
    "field",
    "env",
    "env_aliases",
    "category",
    "type",
    "default",
    "value",
    "set",
    "changed",
    "description",
}


def _settings(**overrides) -> Settings:
    base = {"api_key": OPERATOR, "capture_api_key": CAPTURE, "auth_disabled": False}
    return Settings(**{**base, **overrides})


def _by_field(snap: dict) -> dict[str, dict]:
    return {s["field"]: s for section in snap["sections"] for s in section["settings"]}


def _registry_snapshot(settings: Settings) -> dict:
    return asyncio.run(build_registry(None, settings=settings).get("config").handler())


def test_sections_follow_the_reference_order():
    snap = _registry_snapshot(_settings())
    assert [section["name"] for section in snap["sections"]] == list(SECTIONS)
    meta = {doc.name: doc for doc in collect_fields()}
    for section in snap["sections"]:
        for entry in section["settings"]:
            assert meta[entry["field"]].section == section["name"]


def test_every_setting_appears_exactly_once_with_the_contract_keys():
    snap = _registry_snapshot(_settings())
    fields = [s["field"] for section in snap["sections"] for s in section["settings"]]
    assert sorted(fields) == sorted(Settings.model_fields)
    assert len(fields) == len(set(fields))
    for section in snap["sections"]:
        for entry in section["settings"]:
            assert set(entry) == KEYS, entry["field"]


def test_metadata_matches_the_reference_derivation():
    """Env names, aliases, type and category are the generator's, not a second copy."""
    entries = _by_field(_registry_snapshot(_settings()))
    for doc in collect_fields():
        entry = entries[doc.name]
        assert entry["env"] == doc.env_name
        assert entry["env_aliases"] == list(doc.env_names[1:])
        assert entry["type"] == doc.type_name
        assert entry["category"] == doc.category
        assert entry["description"] == doc.description
    assert entries["xai_api_key"]["env"] == "XAI_API_KEY"
    assert entries["xai_api_key"]["env_aliases"] == ["TRADER_XAI_API_KEY"]
    assert entries["risk_max_position_frac"]["env"] == "TRADER_RISK_MAX_POSITION_FRAC"
    assert entries["risk_max_position_frac"]["env_aliases"] == []


def test_values_are_the_running_settings_and_changed_tracks_the_default():
    entries = _by_field(_registry_snapshot(_settings(risk_max_position_frac=0.25)))
    frac = entries["risk_max_position_frac"]
    assert frac["type"] == "float"
    assert frac["default"] == 0.10 and frac["value"] == 0.25
    assert frac["set"] is True and frac["changed"] is True
    trades = entries["risk_max_trades_per_day"]
    assert trades["value"] == trades["default"] == 20
    assert trades["set"] is False and trades["changed"] is False
    live = entries["alpaca_live_enabled"]
    assert live["category"] == "gate" and live["value"] is False


def test_credentials_never_carry_a_value():
    settings = _settings(xai_api_key=XAI)
    snap = _registry_snapshot(settings)
    entries = _by_field(snap)
    credentials = [e for e in entries.values() if e["category"] == CREDENTIAL]
    assert {e["field"] for e in credentials} >= {"api_key", "xai_api_key", "alpaca_api_secret"}
    for entry in credentials:
        assert entry["value"] is None and entry["default"] is None, entry["field"]
        assert entry["set"] is entry["changed"], entry["field"]
    assert entries["api_key"]["set"] is True
    assert entries["capture_api_key"]["set"] is True
    assert entries["xai_api_key"]["set"] is True
    assert entries["alpaca_api_secret"]["set"] is False
    assert entries["fred_api_key"]["set"] is False
    dumped = json.dumps(snap)
    for secret in (OPERATOR, CAPTURE, XAI):
        assert secret not in dumped
        assert secret[:7] not in dumped and secret[-6:] not in dumped  # not even partially


def test_database_url_password_is_masked():
    url = f"postgresql://trader:{DB_PASSWORD}@db.internal:5432/trader"
    snap = _registry_snapshot(_settings(database_url=url))
    entry = _by_field(snap)["database_url"]
    assert entry["value"] == "postgresql://trader:***@db.internal:5432/trader"
    assert entry["default"] == "postgresql://trader:***@localhost:5544/trader"
    assert entry["changed"] is True  # compared on the real values, not the masked ones
    assert DB_PASSWORD not in json.dumps(snap)


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("http://localhost:8009/v1", "http://localhost:8009/v1"),
        ("https://ghp_tok3n@github.com/x", "https://***@github.com/x"),
        ("https://user:pw@host/v1?api_key=k&mode=a", "https://user:***@host/v1?api_key=***&mode=a"),
        ("postgresql://u@h/d?sslpassword=p", "postgresql://***@h/d?sslpassword=***"),
        ("host=db password=s3cret dbname=trader", "host=db password=*** dbname=trader"),
        ("grok-4.3", "grok-4.3"),
        ("", ""),
    ],
)
def test_mask_secrets(raw: str, masked: str):
    assert mask_secrets(raw) == masked


def test_snapshot_is_pure_metadata_plus_values():
    """The module function and the registry query are the same snapshot."""
    settings = _settings()
    assert snapshot(settings) == _registry_snapshot(settings)


# --- surfaces -----------------------------------------------------------------


def test_capture_key_is_refused_operator_key_is_served(clean_db: None):
    with TestClient(create_app(_settings())) as tc:
        assert tc.get("/config").status_code == 401
        denied = tc.get("/config", headers={"x-api-key": CAPTURE})
        assert denied.status_code == 403
        assert denied.json()["scope"] == CAPTURE_SCOPE
        ok = tc.get("/config", headers={"x-api-key": OPERATOR})
        assert ok.status_code == 200
        assert OPERATOR not in ok.text and CAPTURE not in ok.text
        assert _by_field(ok.json())["api_key"]["set"] is True


def test_config_parity_across_all_surfaces(clean_db: None, monkeypatch: pytest.MonkeyPatch):
    settings = _settings(auth_disabled=True)

    async def via_registry_and_mcp() -> tuple[dict, dict]:
        registry = build_registry(None, settings=settings)
        direct = await registry.get("config").handler()
        async with Client(build_mcp(registry)) as mcp_client:
            tools = await mcp_client.list_tools()
            assert "get_config" in {t.name for t in tools.tools}
            call = await mcp_client.call_tool("get_config", {})
        return direct, call.structured_content

    via_registry, via_mcp = asyncio.run(via_registry_and_mcp())
    with TestClient(create_app(settings)) as tc:
        via_api = tc.get("/config").json()

    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(cli.app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    via_cli = json.loads(result.output)

    assert via_registry == via_api == via_mcp == via_cli


def test_cli_table_marks_changes_and_hides_credentials(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
):
    settings = _settings(auth_disabled=True, risk_max_position_frac=0.25, xai_api_key=XAI)
    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("[Surfaces / auth]") < out.index("[Broker / market data credentials]")
    frac = next(line for line in out.splitlines() if "TRADER_RISK_MAX_POSITION_FRAC" in line)
    assert frac.startswith("*") and "0.25" in frac and "(default 0.1)" in frac
    xai = next(line for line in out.splitlines() if " XAI_API_KEY " in line)
    assert xai.startswith("*") and "(set)" in xai
    assert XAI not in out and OPERATOR not in out
