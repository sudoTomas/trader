"""`trader doctor` preflight — every check exercised over mocked httpx clients, no
live network. The Postgres check runs against the compose test database."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from trader.brokers.alpaca import PAPER_REST
from trader.data.alpaca import AlpacaData
from trader.data.composite import CompositeMarketData
from trader.llm.nemotron import nemotron_client
from trader.llm.registry import LLMRegistry, build_decide_client
from trader.llm.xai_auth import DurableState, XaiCredentialStore
from trader.ops import doctor
from trader.ops.config import Settings
from trader.ops.doctor import (
    CheckResult,
    DoctorReport,
    check_broker,
    check_decide,
    check_market_data,
    check_mode,
    check_postgres,
    check_xai,
    run_doctor,
)

BLANK = Settings(auth_disabled=True)
KEYED = Settings(auth_disabled=True, alpaca_api_key_id="k", alpaca_api_secret="s")

ACCOUNT = {
    "status": "ACTIVE",
    "equity": "100025.31",
    "cash": "98000.00",
    "buying_power": "196000.00",
    "non_marginable_buying_power": "98000.00",
    "trading_blocked": False,
    "account_blocked": False,
}
CRYPTO_QUOTE = {"quotes": {"BTC/USD": {"t": "2026-09-29T14:00:00Z", "bp": 64000.0, "ap": 64010.0}}}


def llm_client(seen: dict, response: dict, *, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(status, json=response)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://spark")


def ok_response(text: str = '{"ok": true}') -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 4},
    }


def rest_client(handler, base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


# --- 1. Postgres -------------------------------------------------------------


async def test_postgres_ok_when_reachable_and_migrated(clean_db: None):
    result = await check_postgres(BLANK)

    assert result.status == "ok"
    assert result.required is True
    assert result.data["pending"] == []
    assert result.data["applied"] >= 7
    assert result.env == ("TRADER_DATABASE_URL",)


async def test_postgres_missing_when_unreachable():
    settings = Settings(auth_disabled=True, database_url="postgresql://trader:trader@127.0.0.1:1/x")

    result = await check_postgres(settings, connect_seconds=2.0)

    assert result.status == "missing"
    assert "TRADER_DATABASE_URL" in result.env
    assert "docker compose" in result.hint


async def test_postgres_degraded_with_pending_migrations_and_never_applies(
    clean_db: None, tmp_path: Path
):
    """Pending files are listed read-only; the doctor must not run them."""
    from trader.store.migrate import MIGRATIONS_DIR

    for path in MIGRATIONS_DIR.glob("*.sql"):
        (tmp_path / path.name).write_text(path.read_text())
    (tmp_path / "9999_doctor_probe.sql").write_text("CREATE TABLE doctor_probe (id int)")

    result = await check_postgres(BLANK, migrations_dir=tmp_path)

    assert result.status == "degraded"
    assert result.data["pending"] == ["9999_doctor_probe.sql"]
    assert "start the API" in result.hint

    import psycopg

    with psycopg.connect(BLANK.database_url) as conn:
        assert conn.execute("SELECT to_regclass('doctor_probe')").fetchone()[0] is None


# --- 2. Decide endpoint ------------------------------------------------------


async def test_decide_ok_on_nemotron_preset_reports_provider_model_latency():
    seen: dict = {}
    registry = LLMRegistry(
        {"decide": [nemotron_client(BLANK, client=llm_client(seen, ok_response()))]}, None
    )

    result = await check_decide(BLANK, registry=registry)

    assert result.status == "ok", result.detail
    assert result.data["provider"] == "nemotron"
    assert result.data["model"] == "nemotron-3-super"
    assert result.data["latency_ms"] >= 0
    assert seen["path"] == "/chat/completions"
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}  # the invariant
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert seen["body"]["max_tokens"] <= 32
    assert result.env == ("TRADER_NEMOTRON_BASE_URL",)


async def test_decide_generic_endpoint_uses_decide_env_names():
    settings = Settings(
        auth_disabled=True,
        decide_base_url="https://api.openai.com/v1",
        decide_model="gpt-4o-mini",
        decide_api_key="sk-test",
    )
    seen: dict = {}
    client = build_decide_client(settings, client=llm_client(seen, ok_response()))

    result = await check_decide(settings, registry=LLMRegistry({"decide": [client]}, None))

    assert result.status == "ok"
    assert result.data["provider"] == "openai_compat"
    assert result.data["model"] == "gpt-4o-mini"
    assert "chat_template_kwargs" not in seen["body"]
    assert result.env == ("TRADER_DECIDE_BASE_URL", "TRADER_DECIDE_MODEL", "TRADER_DECIDE_API_KEY")


async def test_decide_missing_when_endpoint_errors():
    seen: dict = {}
    registry = LLMRegistry(
        {"decide": [nemotron_client(BLANK, client=llm_client(seen, {}, status=503))]}, None
    )

    result = await check_decide(BLANK, registry=registry)

    assert result.status == "missing"
    assert "503" in result.detail
    assert "TRADER_NEMOTRON_BASE_URL" in result.hint


async def test_decide_degraded_when_schema_not_honored():
    seen: dict = {}
    registry = LLMRegistry(
        {"decide": [nemotron_client(BLANK, client=llm_client(seen, ok_response("sure!")))]},
        None,
    )

    result = await check_decide(BLANK, registry=registry)

    assert result.status == "degraded"
    assert "schema" in result.detail


async def test_decide_missing_when_generic_url_has_no_model():
    settings = Settings(auth_disabled=True, decide_base_url="https://api.openai.com/v1")

    result = await check_decide(settings)  # no registry: construction itself must fail

    assert result.status == "missing"
    assert "TRADER_DECIDE_MODEL" in result.detail


# --- 3. Broker ---------------------------------------------------------------


async def test_broker_missing_without_keys():
    result = await check_broker(BLANK)

    assert result.status == "missing"
    assert result.env == ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET")


async def test_broker_ok_reports_equity_and_status():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=ACCOUNT)

    result = await check_broker(KEYED, client=rest_client(handler, PAPER_REST))

    assert result.status == "ok", result.detail
    assert seen["url"] == f"{PAPER_REST}/v2/account"
    assert result.data["equity"] == 100025.31
    assert result.data["account_status"] == "ACTIVE"
    assert result.data["host"] == PAPER_REST


async def test_broker_missing_on_rejected_keys():
    result = await check_broker(
        KEYED,
        client=rest_client(
            lambda r: httpx.Response(401, json={"message": "forbidden"}), PAPER_REST
        ),
    )

    assert result.status == "missing"
    assert "401" in result.detail


async def test_broker_degraded_when_account_not_tradable():
    blocked = {**ACCOUNT, "trading_blocked": True}

    result = await check_broker(
        KEYED, client=rest_client(lambda r: httpx.Response(200, json=blocked), PAPER_REST)
    )

    assert result.status == "degraded"
    assert "blocked" in result.detail


async def test_broker_live_flag_warns_and_never_builds_a_live_client(monkeypatch):
    constructed: dict = {}

    class SpyBroker:
        def __init__(self, key_id, secret, *, live=False, live_enabled=False, client=None):
            constructed.update(live=live, live_enabled=live_enabled)

        async def get_account(self):
            from trader.brokers.base import AccountSnapshot

            return AccountSnapshot(1.0, 1.0, 1.0, 1.0, "ACTIVE", True)

        async def close(self):
            pass

    monkeypatch.setattr(doctor, "AlpacaBroker", SpyBroker)
    settings = Settings(
        auth_disabled=True, alpaca_api_key_id="k", alpaca_api_secret="s", alpaca_live_enabled=True
    )

    result = await check_broker(settings)

    assert constructed == {"live": False, "live_enabled": False}
    assert result.status == "ok"
    assert result.warnings and "TRADER_ALPACA_LIVE_ENABLED" in result.warnings[0]


# --- 4. Market data ----------------------------------------------------------


def crypto_market_data(handler) -> CompositeMarketData:
    from trader.core.symbols import AssetClass

    alpaca = AlpacaData("k", "s", client=rest_client(handler, "https://data.alpaca.markets"))
    return CompositeMarketData({AssetClass.CRYPTO: [alpaca], AssetClass.US_EQUITY: [alpaca]})


async def test_market_data_missing_without_keys():
    result = await check_market_data(BLANK)

    assert result.status == "missing"
    assert result.env == ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET")


async def test_market_data_ok_quotes_first_loop_symbol():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["symbols"] = request.url.params["symbols"]
        return httpx.Response(200, json=CRYPTO_QUOTE)

    result = await check_market_data(KEYED, market_data=crypto_market_data(handler))

    assert result.status == "ok", result.detail
    assert seen["path"] == "/v1beta3/crypto/us/latest/quotes"
    assert seen["symbols"] == "BTC/USD"
    assert result.data["symbol"] == "BTC/USD"
    assert result.data["mid"] == 64005.0


async def test_market_data_missing_when_provider_fails():
    result = await check_market_data(
        KEYED, market_data=crypto_market_data(lambda r: httpx.Response(403, text="forbidden"))
    )

    assert result.status == "missing"
    assert "403" in result.detail


async def test_market_data_missing_for_unknown_symbol():
    settings = Settings(
        auth_disabled=True, alpaca_api_key_id="k", alpaca_api_secret="s", loop_symbols="DOGE/USD"
    )

    result = await check_market_data(settings, market_data=crypto_market_data(lambda r: None))

    assert result.status == "missing"
    assert result.env == ("TRADER_LOOP_SYMBOLS",)
    assert "DOGE/USD" in result.detail


# --- 5. xAI (optional) -------------------------------------------------------


def test_xai_degraded_when_absent():
    result = check_xai(BLANK)

    assert result.status == "degraded"
    assert result.required is False
    assert "fall back to the decide model" in result.detail
    assert "sentiment" in result.detail
    assert result.env == ("XAI_API_KEY", "TRADER_XAI_TOKEN_KEY")


def test_xai_ok_via_api_key():
    result = check_xai(Settings(auth_disabled=True, xai_api_key="xai-test"))

    assert result.status == "ok"
    assert result.data["source"] == "api_key"


def test_xai_ok_via_oauth_store(tmp_path: Path):
    key = Fernet.generate_key().decode()
    path = tmp_path / "xai_token.enc"
    XaiCredentialStore(path, key).save(
        DurableState("refresh", "client", "https://auth.x.ai/oauth/token")
    )

    result = check_xai(Settings(auth_disabled=True, xai_token_key=key, xai_token_path=str(path)))

    assert result.status == "ok"
    assert result.data["source"] == "oauth"


def test_xai_degraded_when_store_absent_and_no_key(tmp_path: Path):
    key = Fernet.generate_key().decode()
    settings = Settings(
        auth_disabled=True, xai_token_key=key, xai_token_path=str(tmp_path / "none.enc")
    )

    result = check_xai(settings)

    assert result.status == "degraded"
    assert "trader xai login" in result.hint


# --- 6. Mode summary ---------------------------------------------------------


def test_mode_summary_prints_flags_without_judging():
    settings = Settings(
        auth_disabled=True,
        shadow_mode=True,
        loop_enabled=True,
        loop_symbols="BTC/USD, ETH/USD",
        desk_enabled=False,
    )

    result = check_mode(settings)

    assert result.status == "ok"
    assert result.data == {
        "shadow_mode": True,
        "loop_enabled": True,
        "symbols": ["BTC/USD", "ETH/USD"],
        "shadow_symbols": [],
        "desk_enabled": False,
        "briefs_enabled": False,
        "sentiment_trigger_enabled": False,
        "auth": "disabled",
        "api_base_url": "http://localhost:8080",
    }
    assert "shadow=on" in result.detail and "loop=on" in result.detail
    assert result.warnings == []


def test_mode_warns_when_auth_disabled_on_non_loopback_bind():
    settings = Settings(auth_disabled=True, api_base_url="http://0.0.0.0:8080")

    result = check_mode(settings)

    assert result.status == "degraded"
    assert result.warnings and "TRADER_AUTH_DISABLED" in result.warnings[0]


def test_mode_degraded_when_auth_neither_keyed_nor_disabled():
    """Settings.require_auth() refuses boot here; say so before the user starts uvicorn."""
    result = check_mode(Settings(auth_disabled=False))

    assert result.status == "degraded"
    assert result.data["auth"] == "unset"
    assert result.env == ("TRADER_API_KEY", "TRADER_AUTH_DISABLED")
    assert "refuses to boot" in result.detail


def test_mode_reports_api_key_auth():
    result = check_mode(
        Settings(api_key="secret", auth_disabled=False, api_base_url="http://0.0.0.0:8080")
    )

    assert result.data["auth"] == "api-key"
    assert result.warnings == []


# --- Report + CLI ------------------------------------------------------------


def _report(*checks: CheckResult) -> DoctorReport:
    return DoctorReport(checks=list(checks))


def _check(name: str, status: str, *, required: bool = True, **extra) -> CheckResult:
    return CheckResult(
        name=name, status=status, required=required, detail=f"{name} {status}", **extra
    )


def test_exit_code_one_only_when_a_required_check_is_missing():
    assert (
        _report(_check("postgres", "ok"), _check("xai", "degraded", required=False)).exit_code == 0
    )
    assert _report(_check("postgres", "degraded"), _check("decide", "ok")).exit_code == 0
    assert _report(_check("xai", "missing", required=False)).exit_code == 0
    assert _report(_check("broker", "missing")).exit_code == 1


def test_report_json_shape_and_table():
    report = _report(
        _check("postgres", "ok"),
        _check(
            "broker",
            "missing",
            env=("ALPACA_API_KEY_ID", "ALPACA_API_SECRET"),
            hint="paper keys from the Alpaca dashboard",
            warnings=["TRADER_ALPACA_LIVE_ENABLED=true: doctor probes PAPER only"],
        ),
    )

    payload = report.to_dict()
    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert [c["name"] for c in payload["checks"]] == ["postgres", "broker"]
    assert payload["checks"][1]["env"] == ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET"]
    assert payload["warnings"] == ["TRADER_ALPACA_LIVE_ENABLED=true: doctor probes PAPER only"]

    table = report.render()
    assert "WARNING" in table
    assert "broker" in table and "missing" in table
    assert "ALPACA_API_KEY_ID" in table
    assert "exit 1" in table


async def test_run_doctor_blank_env_fails_closed_without_network(clean_db: None):
    """A fresh checkout with only Postgres up: required checks missing → exit 1, no
    live calls (the LLM probe goes through an injected failing client)."""
    seen: dict = {}
    registry = LLMRegistry(
        {"decide": [nemotron_client(BLANK, client=llm_client(seen, {}, status=502))]}, None
    )

    report = await run_doctor(BLANK, registry=registry)

    by_name = {c.name: c for c in report.checks}
    assert list(by_name) == ["postgres", "decide", "broker", "market_data", "xai", "mode"]
    assert by_name["postgres"].status == "ok"
    assert by_name["decide"].status == "missing"
    assert by_name["broker"].status == "missing"
    assert by_name["market_data"].status == "missing"
    assert by_name["xai"].status == "degraded"
    assert by_name["mode"].status == "ok"
    assert report.exit_code == 1


async def test_run_doctor_shadow_quickstart_profile_exits_zero(clean_db: None):
    """The quickstart profile: Postgres + one decide endpoint + Alpaca paper keys, no xAI."""
    settings = Settings(
        auth_disabled=True,
        alpaca_api_key_id="k",
        alpaca_api_secret="s",
        shadow_mode=True,
        loop_enabled=True,
    )
    seen: dict = {}
    registry = LLMRegistry(
        {"decide": [nemotron_client(settings, client=llm_client(seen, ok_response()))]}, None
    )
    broker = rest_client(lambda r: httpx.Response(200, json=ACCOUNT), PAPER_REST)
    data = crypto_market_data(lambda r: httpx.Response(200, json=CRYPTO_QUOTE))

    report = await run_doctor(settings, registry=registry, broker_client=broker, market_data=data)

    assert [c.status for c in report.checks] == ["ok", "ok", "ok", "ok", "degraded", "ok"]
    assert report.exit_code == 0


def test_cli_doctor_json_and_table(monkeypatch: pytest.MonkeyPatch):
    from trader.surfaces.cli import main as cli

    report = _report(
        _check("postgres", "ok", data={"applied": 7, "pending": []}),
        _check("decide", "missing", env=("TRADER_NEMOTRON_BASE_URL",), hint="start vLLM"),
    )

    async def fake_run_doctor(settings, **kwargs):
        assert isinstance(settings, Settings)
        return report

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)

    result = CliRunner().invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["exit_code"] == 1
    assert payload["checks"][1]["hint"] == "start vLLM"

    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "decide" in result.output and "missing" in result.output
    assert "TRADER_NEMOTRON_BASE_URL" in result.output


def test_cli_doctor_exits_zero_when_only_degraded(monkeypatch: pytest.MonkeyPatch):
    from trader.surfaces.cli import main as cli

    async def fake_run_doctor(settings, **kwargs):
        return _report(_check("postgres", "ok"), _check("xai", "degraded", required=False))

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)

    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "exit 0" in result.output
