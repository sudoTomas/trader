"""Live layered pricing (#105): every model has a price, and local models cost nothing.

Order per (Provider, model): local Provider → $0; a manual override; the Provider's own
models list when it carries prices (OpenRouter); LiteLLM's public catalog, fetched at
most once a day; else unknown. Seams: PriceBook (through ProviderDesk), metering, the
HTTP API with auth, and MCP + CLI parity. Catalog and Provider HTTP are mocked at the
transport.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from trader.llm.pricing import CATALOG_URL, DAY_SECONDS
from trader.llm.providers import ProviderDesk, ProviderError
from trader.store.db import Database

SECRETS_KEY = Fernet.generate_key().decode()
LLAMA = "meta-llama/llama-3.3-70b-instruct"

CATALOG = {
    "sample_spec": {"input_cost_per_token": 0, "output_cost_per_token": 0},
    "gpt-5": {"input_cost_per_token": 1.25e-06, "output_cost_per_token": 1e-05},
    "openrouter/" + LLAMA: {"input_cost_per_token": 9e-07, "output_cost_per_token": 9e-07},
    "groq/llama-3.3-70b-versatile": {
        "input_cost_per_token": 5.9e-07,
        "output_cost_per_token": 7.9e-07,
    },
    "llama-3.3-70b-versatile": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06},
}
OPENROUTER_MODELS = {
    "data": [{"id": LLAMA, "pricing": {"prompt": "0.00000013", "completion": "0.0000004"}}]
}


class Web:
    """The catalog, OpenRouter and Ollama at the transport; counts each fetch."""

    def __init__(self) -> None:
        self.catalog: dict[str, Any] | int = CATALOG
        self.hits: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits[url] = self.hits.get(url, 0) + 1
        if url == CATALOG_URL:
            if isinstance(self.catalog, int):
                return httpx.Response(self.catalog)
            return httpx.Response(200, json=self.catalog)
        if url == "https://openrouter.ai/api/v1/models":
            return httpx.Response(200, json=OPENROUTER_MODELS)
        if url.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "llama-3.3-70b-versatile"}]})
        if url == "https://api.groq.com/openai/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}]})
        return httpx.Response(404)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def desk(db: Database, web: Web, clock: Clock | None = None) -> ProviderDesk:
    return ProviderDesk(
        db,
        secrets_key=SECRETS_KEY,
        http=httpx.AsyncClient(transport=httpx.MockTransport(web)),
        clock=clock or Clock(),
    )


def per_million(price: dict[str, Any]) -> tuple[float, float, str]:
    return (round(price["price_in"], 4), round(price["price_out"], 4), price["source"])


# --- PriceBook ------------------------------------------------------------------------------


async def test_a_local_model_is_free_even_when_the_catalog_prices_it(db: Database):
    providers = desk(db, Web())
    await providers.add(name="spark", preset="ollama", base_url="http://spark:11434/v1")

    price = await providers.price("spark", "llama-3.3-70b-versatile")

    assert per_million(price) == (0.0, 0.0, "local")


async def test_the_same_open_weight_model_is_paid_on_a_hosted_provider(db: Database):
    providers = desk(db, Web())
    await providers.add(name="spark", preset="ollama", base_url="http://spark:11434/v1")
    await providers.add(name="groq", preset="groq", api_key="gsk-key-12345")

    local = await providers.price("spark", "llama-3.3-70b-versatile")
    hosted = await providers.price("groq", "llama-3.3-70b-versatile")

    assert per_million(local) == (0.0, 0.0, "local")
    # The provider-prefixed catalog entry wins over the bare id.
    assert per_million(hosted) == (0.59, 0.79, "catalog")


async def test_openrouters_listed_price_wins_over_the_catalog(db: Database):
    providers = desk(db, Web())
    await providers.add(name="or", preset="openrouter", api_key="sk-or-key-12345")

    assert per_million(await providers.price("or", LLAMA)) == (0.13, 0.4, "provider")


async def test_an_override_wins_over_provider_and_catalog_and_is_audited(db: Database):
    providers = desk(db, Web())
    await providers.add(name="or", preset="openrouter", api_key="sk-or-key-12345")
    await providers.add(name="openai", preset="openai", api_key="sk-key-12345")

    await providers.set_price("or", LLAMA, price_in=0.5, price_out=0.6)
    await providers.set_price("openai", "gpt-5", price_in=1.0, price_out=8.0)

    assert per_million(await providers.price("or", LLAMA)) == (0.5, 0.6, "override")
    assert per_million(await providers.price("openai", "gpt-5")) == (1.0, 8.0, "override")
    assert (await providers.clear_price("openai", "gpt-5"))["cleared"] is True
    assert per_million(await providers.price("openai", "gpt-5")) == (1.25, 10.0, "catalog")
    actions = [
        r["action"]
        for r in await db.fetch_all(
            "SELECT action FROM audit_events WHERE category = 'providers' ORDER BY id"
        )
    ]
    assert actions[-3:] == ["set_model_price", "set_model_price", "clear_model_price"]


async def test_an_override_cannot_price_a_local_model(db: Database):
    providers = desk(db, Web())
    await providers.add(name="spark", preset="ollama", base_url="http://spark:11434/v1")
    with pytest.raises(ProviderError, match="local models are free"):
        await providers.set_price("spark", "qwen3:8b", price_in=1.0, price_out=1.0)
    with pytest.raises(ProviderError, match="non-negative"):
        await providers.set_price("spark", "qwen3:8b", price_in=-1.0, price_out=1.0)


async def test_an_unknown_hosted_model_is_unpriced(db: Database):
    providers = desk(db, Web())
    await providers.add(name="openai", preset="openai", api_key="sk-key-12345")
    assert per_million(await providers.price("openai", "gpt-9-preview")) == (
        0.0,
        0.0,
        "unknown",
    )


async def test_the_catalog_is_fetched_once_a_day_and_a_failure_keeps_the_cache(db: Database):
    web, clock = Web(), Clock()
    providers = desk(db, web, clock)
    await providers.add(name="openai", preset="openai", api_key="sk-key-12345")

    await providers.price("openai", "gpt-5")
    clock.now += DAY_SECONDS - 1
    await providers.price("openai", "gpt-5")
    assert web.hits[CATALOG_URL] == 1

    web.catalog = 503  # a day later the catalog is down
    clock.now += 2
    stale = await providers.price("openai", "gpt-5")
    clock.now += 60
    await providers.price("openai", "gpt-5")

    assert web.hits[CATALOG_URL] == 2  # one attempt, not one per call
    assert per_million(stale) == (1.25, 10.0, "catalog")


async def test_listed_models_carry_their_price_and_source(db: Database):
    providers = desk(db, Web())
    await providers.add(name="groq", preset="groq", api_key="gsk-key-12345")
    await providers.add(name="spark", preset="ollama", base_url="http://spark:11434/v1")

    assert await providers.models("groq") == [
        {
            "id": "llama-3.3-70b-versatile",
            "price_in": pytest.approx(0.59),
            "price_out": pytest.approx(0.79),
            "source": "catalog",
        }
    ]
    assert (await providers.models("spark"))[0]["source"] == "local"


# --- metering -------------------------------------------------------------------------------


async def test_a_profile_served_call_meters_at_the_picker_price(db: Database):
    from trader.llm.profiles import ProfileDesk
    from trader.llm.registry import LLMRegistry
    from trader.llm.usage import UsageMeter
    from trader.store.repos import UsageRepo

    decide = json.dumps(
        {"action": "hold", "conviction": 0.1, "rationale": "p", "side": None, "notional_usd": None}
    )

    def chat(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": decide}}],
                    "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000},
                },
            )
        return Web()(request)

    providers = ProviderDesk(
        db,
        secrets_key=SECRETS_KEY,
        http=httpx.AsyncClient(transport=httpx.MockTransport(chat)),
    )
    profiles = ProfileDesk(db, providers, transport=httpx.MockTransport(chat))
    await providers.add(name="openai", preset="openai", api_key="sk-key-12345")
    await profiles.create("trial")
    await profiles.set_chain("trial", "decide", [{"provider": "openai", "model": "gpt-5"}])
    registry = LLMRegistry({}, UsageMeter(UsageRepo(db)), profiles=profiles)

    completion = await registry.complete("decide", [{"role": "user", "content": "x"}])

    expected = 1.25 + 0.1 * 10.0  # 1M prompt + 100k completion at $1.25 / $10 per 1M
    assert completion.cost_usd == pytest.approx(expected)
    row = await db.fetch_one("SELECT cost_usd FROM usage_daily WHERE role = 'decide'")
    assert float(row["cost_usd"]) == pytest.approx(expected)
    routes = await profiles.routes()
    assert routes["decide"]["chain"][0]["price"] == {
        "price_in": pytest.approx(1.25),
        "price_out": pytest.approx(10.0),
        "source": "catalog",
    }


# --- the HTTP API and its auth ----------------------------------------------------------------

OPERATOR = {"X-API-Key": "operator-key"}
CAPTURE = {"X-API-Key": "capture-key"}


def api_settings():
    from trader.ops.config import Settings

    return Settings(
        auth_disabled=False,
        api_key="operator-key",
        capture_api_key="capture-key",
        secrets_key=SECRETS_KEY,
    )


def test_api_sets_and_clears_an_override(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:
        added = tc.post(
            "/providers",
            json={"name": "or", "preset": "openrouter", "api_key": "sk-or-key-12345"},
            headers=OPERATOR,
        )
        assert added.status_code == 200, added.text
        body = {"model": LLAMA, "price_in": 0.5, "price_out": 0.6}
        priced = tc.post("/providers/or/prices", json=body, headers=OPERATOR)
        assert priced.status_code == 200, priced.text
        assert priced.json() == {
            "model": LLAMA,
            "price_in": 0.5,
            "price_out": 0.6,
            "source": "override",
        }
        cleared = tc.post("/providers/or/prices/clear", json={"model": LLAMA}, headers=OPERATOR)
        assert cleared.json() == {"provider": "or", "model": LLAMA, "cleared": True}
        bad = tc.post("/providers/or/prices", json={**body, "price_in": -1}, headers=OPERATOR)
        assert bad.status_code == 422
        missing = tc.post("/providers/ghost/prices", json=body, headers=OPERATOR)
        assert missing.status_code == 404


def test_the_capture_key_cannot_reach_price_routes(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:
        for path, body in (
            ("/providers/x/prices", {"model": "m", "price_in": 1, "price_out": 1}),
            ("/providers/x/prices/clear", {"model": "m"}),
        ):
            assert tc.post(path, json=body, headers=CAPTURE).status_code == 403


# --- MCP and CLI parity -----------------------------------------------------------------------


def test_overrides_are_identical_through_every_door(clean_db: None, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    settings = Settings(auth_disabled=True, secrets_key=SECRETS_KEY)
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    runner = CliRunner()
    runner.invoke(
        cli.app,
        ["provider", "add", "openai", "--preset", "openai", "--key-stdin"],
        input="sk-key-12345\n",
    )

    via_cli = runner.invoke(
        cli.app, ["provider", "price", "openai", "gpt-5", "--in", "2", "--out", "9"]
    )
    assert via_cli.exit_code == 0, via_cli.output
    assert json.loads(via_cli.output)["source"] == "override"

    async def via_mcp() -> dict[str, Any]:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            offline = httpx.AsyncClient(transport=httpx.MockTransport(Web()))
            providers = ProviderDesk(db, secrets_key=SECRETS_KEY, http=offline)
            registry = build_registry(db, providers=providers)
            async with Client(build_mcp(registry)) as mcp_client:
                tools = {t.name for t in (await mcp_client.list_tools()).tools}
                assert {"set_model_price", "clear_model_price"} <= tools
                cleared = await mcp_client.call_tool(
                    "clear_model_price", {"name": "openai", "model": "gpt-5"}
                )
            return cleared.structured_content
        finally:
            await db.close()

    cleared = asyncio.run(via_mcp())
    assert cleared == {"provider": "openai", "model": "gpt-5", "cleared": True}
    again = runner.invoke(cli.app, ["provider", "unprice", "openai", "gpt-5"])
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["cleared"] is False  # nothing left to clear
