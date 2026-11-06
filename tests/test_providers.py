"""Providers (#103): where models are served from, with their keys encrypted at rest and
never readable back. Seams: ProviderDesk, the service registry, the HTTP API (with the
auth middleware), and MCP + CLI parity. Provider HTTP is mocked at the transport.
"""

from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet

from trader.llm.providers import ProviderDesk, ProviderError, ProviderNotFound
from trader.store.db import Database

SECRETS_KEY = Fernet.generate_key().decode()
OPENROUTER_KEY = "sk-or-v1-secret-abcd"


def desk(db: Database, **kwargs) -> ProviderDesk:
    return ProviderDesk(db, secrets_key=SECRETS_KEY, **kwargs)


async def test_a_hosted_provider_lists_with_its_key_masked(db: Database):
    added = await desk(db).add(name="openrouter", preset="openrouter", api_key=OPENROUTER_KEY)

    assert added == {
        "name": "openrouter",
        "preset": "openrouter",
        "kind": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "local": False,
        "key": "••••abcd",
        "extra_body": {},
    }
    assert await desk(db).list() == [added]
    # At rest the key is ciphertext: the plaintext appears nowhere in the row.
    row = await db.fetch_one("SELECT * FROM llm_providers WHERE name = 'openrouter'")
    assert OPENROUTER_KEY not in str(row)


async def test_local_providers_need_no_key_and_no_secrets_key(db: Database):
    keyless = ProviderDesk(db, secrets_key="")
    added = await keyless.add(
        name="spark-ollama", preset="ollama", base_url="http://spark:11434/v1"
    )
    assert (added["local"], added["key"], added["base_url"]) == (
        True,
        None,
        "http://spark:11434/v1",
    )
    with pytest.raises(ProviderError, match="TRADER_SECRETS_KEY"):
        await keyless.add(name="openai", preset="openai", api_key="sk-x")


async def test_a_malformed_secrets_key_refuses_keys_but_never_boot(db: Database):
    broken = ProviderDesk(db, secrets_key="not-a-fernet-key")
    await broken.add(name="ollama", preset="ollama")
    with pytest.raises(ProviderError, match="TRADER_SECRETS_KEY is not a valid Fernet key"):
        await broken.add(name="openai", preset="openai", api_key="sk-x")


async def test_a_short_key_shows_no_characters_at_all(db: Database):
    added = await desk(db).add(name="lmstudio", preset="lmstudio", api_key="abc")
    assert added["key"] == "••••"
    rotated = await desk(db).set_key("lmstudio", "lm-studio-key-4321")
    assert rotated["key"] == "••••4321"


async def test_bad_input_is_refused_before_anything_is_stored(db: Database):
    d = desk(db)
    with pytest.raises(ProviderError, match="unknown preset"):
        await d.add(name="x", preset="nope")
    with pytest.raises(ProviderError, match="needs an API key"):
        await d.add(name="openai", preset="openai")
    with pytest.raises(ProviderError, match="name"):
        await d.add(name="Bad Name!", preset="ollama")
    with pytest.raises(ProviderError, match="custom needs a base_url"):
        await d.add(name="mine", preset="custom")
    await d.add(name="ollama", preset="ollama")
    with pytest.raises(ProviderError, match="already exists"):
        await d.add(name="ollama", preset="ollama")
    assert [p["name"] for p in await d.list()] == ["ollama"]


async def test_xai_may_sign_with_the_subscription_login_instead_of_a_key(db: Database):
    added = await desk(db).add(name="grok", preset="xai")

    assert (added["kind"], added["local"], added["key"]) == ("xai", False, None)
    presets = ProviderDesk.presets()
    assert (presets["xai"]["needs_key"], presets["xai"]["key_optional"]) == (True, True)
    assert presets["openai"]["key_optional"] is False


async def test_a_provider_on_a_loopback_or_private_address_is_local(db: Database):
    d = desk(db)
    kev = await d.add(
        name="kev",
        preset="jev",
        api_key="kev-key-9b9b",
        base_url="http://127.0.0.1:8011/v1/systemone",
    )
    lan = await d.add(
        name="lan-vllm", preset="custom", base_url="http://192.168.1.50:8009/v1", api_key="k"
    )
    hosted = await d.add(name="together", preset="together", api_key="tg-key")
    assert (kev["local"], kev["key"]) == (True, "••••9b9b")
    assert lan["local"] is True
    assert hosted["local"] is False


async def test_changes_are_audited_without_the_key(db: Database):
    d = desk(db)
    await d.add(name="openrouter", preset="openrouter", api_key=OPENROUTER_KEY)
    replaced = await d.set_key("openrouter", "sk-or-v1-rotated-wxyz")
    assert replaced["key"] == "••••wxyz"
    renamed = await d.rename("openrouter", "or")
    assert renamed["name"] == "or"
    assert await d.remove("or") == {"removed": True, "name": "or"}
    assert await d.list() == []

    rows = await db.fetch_all(
        "SELECT actor, category, action, subject, payload FROM audit_events ORDER BY id"
    )
    assert [(r["category"], r["action"], r["subject"]) for r in rows] == [
        ("providers", "add_provider", "openrouter"),
        ("providers", "set_provider_key", "openrouter"),
        ("providers", "rename_provider", "or"),
        ("providers", "remove_provider", "or"),
    ]
    assert rows[2]["payload"] == {"from": "openrouter"}
    trail = str(rows)
    assert OPENROUTER_KEY not in trail and "rotated" not in trail


async def test_unknown_provider_is_a_lookup_error(db: Database):
    d = desk(db)
    for call in (d.set_key("ghost", "k"), d.rename("ghost", "g2"), d.remove("ghost")):
        with pytest.raises(ProviderNotFound):
            await call


NO_REASONING = {"reasoning": {"enabled": False}}


async def test_extra_body_round_trips_through_the_service_and_is_audited(db: Database):
    from trader.surfaces.services import build_registry

    registry = build_registry(db, providers=desk(db))
    await registry.get("add_provider").handler(
        name="openrouter", preset="openrouter", api_key=OPENROUTER_KEY
    )

    set_ = await registry.get("set_provider_extra_body").handler(
        name="openrouter", extra_body=NO_REASONING
    )

    assert set_["extra_body"] == NO_REASONING
    [listed] = await registry.get("providers").handler()
    assert listed["extra_body"] == NO_REASONING
    # A JSON string is read the way TRADER_DECIDE_EXTRA_BODY is; {} (or blank) clears it.
    as_text = await desk(db).set_extra_body("openrouter", '{"provider": {"sort": "price"}}')
    assert as_text["extra_body"] == {"provider": {"sort": "price"}}
    cleared = await desk(db).set_extra_body("openrouter", {})
    assert cleared["extra_body"] == {}

    rows = await db.fetch_all(
        "SELECT action, subject, payload FROM audit_events"
        " WHERE action = 'set_provider_extra_body' ORDER BY id"
    )
    assert [(r["subject"], r["payload"]) for r in rows] == [
        ("openrouter", {"extra_body": NO_REASONING}),
        ("openrouter", {"extra_body": {"provider": {"sort": "price"}}}),
        ("openrouter", {"extra_body": {}}),
    ]
    assert OPENROUTER_KEY not in str(rows)


async def test_extra_body_refuses_what_must_not_reach_a_vendor(db: Database):
    d = ProviderDesk(db, secrets_key=SECRETS_KEY, nemotron_base_url="http://spark:8009/v1")
    await d.add(name="openrouter", preset="openrouter", api_key=OPENROUTER_KEY)
    await d.add(name="claude", preset="anthropic", api_key="sk-ant-key-1234")
    await d.add(name="nemo", preset="custom", base_url="http://spark:8009/v1/")

    with pytest.raises(ProviderError, match="chat_template_kwargs"):
        await d.set_extra_body("openrouter", {"chat_template_kwargs": {"enable_thinking": True}})
    for bad in ("[1, 2]", "not json", '"text"'):
        with pytest.raises(ProviderError, match="JSON object"):
            await d.set_extra_body("openrouter", bad)
    with pytest.raises(ProviderError, match="OpenAI-chat"):
        await d.set_extra_body("claude", NO_REASONING)
    with pytest.raises(ProviderNotFound):
        await d.set_extra_body("ghost", NO_REASONING)
    assert {p["name"]: p["extra_body"] for p in await d.list()} == {
        "claude": {},
        "nemo": {},
        "openrouter": {},
    }

    # On the Nemotron preset's own endpoint the template kwargs are its business.
    kwargs = {"chat_template_kwargs": {"reasoning_budget": 0}}
    assert (await d.set_extra_body("nemo", kwargs))["extra_body"] == kwargs
    # With no Nemotron endpoint configured, no Provider is it.
    with pytest.raises(ProviderError, match="chat_template_kwargs"):
        await desk(db).set_extra_body("nemo", kwargs)
    saved = await db.fetch_all(
        "SELECT 1 FROM audit_events WHERE action = 'set_provider_extra_body'"
    )
    assert len(saved) == 1  # refusals store and audit nothing


def provider_http(seen: list, *, status: int = 200) -> httpx.AsyncClient:
    """Mock Provider endpoints: OpenAI-style /models and Ollama's /api/tags."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if status != 200:
            return httpx.Response(status, text=f"denied for {OPENROUTER_KEY}")
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.3:70b"}]}
            )
        return httpx.Response(
            200, json={"data": [{"id": "openai/gpt-5"}, {"id": "anthropic/claude-opus-5-5"}]}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_models_come_live_from_the_provider_and_are_cached(db: Database):
    seen: list = []
    d = desk(db, http=provider_http(seen))
    await d.add(name="openrouter", preset="openrouter", api_key=OPENROUTER_KEY)
    await d.add(name="ollama", preset="ollama", base_url="http://spark:11434/v1")

    def ids(models: list[dict]) -> list[str]:
        return [m["id"] for m in models]

    def listings() -> list:
        # The model-list fetches, not the price sources (#105), which go unauthenticated.
        return [s for s in seen if s[1] or s[0].endswith("/api/tags")]

    assert ids(await d.models("openrouter")) == ["anthropic/claude-opus-5-5", "openai/gpt-5"]
    assert listings()[0] == ("https://openrouter.ai/api/v1/models", f"Bearer {OPENROUTER_KEY}")
    assert ids(await d.models("ollama")) == ["llama3.3:70b", "qwen3:8b"]
    assert listings()[1] == ("http://spark:11434/api/tags", None)
    await d.models("openrouter")
    assert len(listings()) == 2  # served from the short cache


async def test_model_listing_errors_carry_the_status_only(db: Database):
    d = desk(db, http=provider_http([], status=401))
    await d.add(name="openrouter", preset="openrouter", api_key=OPENROUTER_KEY)
    with pytest.raises(ProviderError) as exc:
        await d.models("openrouter")
    assert "401" in str(exc.value) and OPENROUTER_KEY not in str(exc.value)
    with pytest.raises(ProviderNotFound):
        await d.models("ghost")


async def test_discovery_reports_local_servers_that_answer_and_adds_nothing(db: Database):
    def handler(request: httpx.Request) -> httpx.Response:
        if (request.url.port, request.url.path) == (11434, "/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        if (request.url.port, request.url.path) == (1234, "/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen2.5-7b"}, {"id": "phi-4"}]})
        if request.url.port == 8080:  # the trader's own API: answers, but is no model server
            return httpx.Response(404, json={"detail": "Not Found"})
        raise httpx.ConnectError("refused", request=request)

    d = desk(db, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    found = await d.discover()
    assert found == [
        {"preset": "lmstudio", "base_url": "http://localhost:1234/v1", "models": 2},
        {"preset": "ollama", "base_url": "http://localhost:11434/v1", "models": 1},
    ]
    assert await d.list() == []


# --- the service registry -------------------------------------------------------------------

PROVIDER_CAPABILITIES = {
    "providers": "query",
    "provider_presets": "query",
    "provider_models": "query",
    "discover_providers": "query",
    "add_provider": "command",
    "set_provider_key": "command",
    "set_provider_extra_body": "command",
    "rename_provider": "command",
    "remove_provider": "command",
}


async def test_provider_capabilities_are_registered_once_for_every_surface(db: Database):
    from trader.surfaces.services import build_registry

    registry = build_registry(db, providers=desk(db))
    kinds = {c.name: c.kind for c in registry.all() if c.name in PROVIDER_CAPABILITIES}
    assert kinds == PROVIDER_CAPABILITIES

    added = await registry.get("add_provider").handler(
        name="openrouter", preset="openrouter", api_key=OPENROUTER_KEY
    )
    assert added["key"] == "••••abcd"
    assert await registry.get("providers").handler() == [added]
    presets = await registry.get("provider_presets").handler()
    assert presets["ollama"] == {
        "kind": "openai_compat",
        "base_url": "http://localhost:11434/v1",
        "local": True,
        "needs_key": False,
        "key_optional": False,
        "default_model": None,
    }
    assert presets["custom"]["needs_key"] is False


async def test_store_less_builds_have_no_provider_capabilities():
    from trader.surfaces.services import build_registry

    names = {c.name for c in build_registry().all()}
    assert not names & set(PROVIDER_CAPABILITIES)


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


def test_api_adds_lists_and_never_returns_a_key(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:
        body = {"name": "openrouter", "preset": "openrouter", "api_key": OPENROUTER_KEY}
        added = tc.post("/providers", json=body, headers=OPERATOR)
        assert added.status_code == 200, added.text
        rotated = tc.post(
            "/providers/openrouter/key", json={"api_key": "sk-new-9999"}, headers=OPERATOR
        )
        renamed = tc.post("/providers/openrouter/rename", json={"new_name": "or"}, headers=OPERATOR)
        listed = tc.get("/providers", headers=OPERATOR)
        presets = tc.get("/providers/presets", headers=OPERATOR)
        audit = tc.get("/audit", headers=OPERATOR)
        bodies = [added, rotated, renamed, listed, presets, audit]
        assert all(r.status_code == 200 for r in bodies), [r.text for r in bodies]
        assert listed.json() == [{**added.json(), "name": "or", "key": "••••9999"}]
        assert not any(OPENROUTER_KEY in r.text or "sk-new" in r.text for r in bodies)

        assert tc.delete("/providers/or", headers=OPERATOR).json() == {
            "removed": True,
            "name": "or",
        }
        assert tc.get("/providers", headers=OPERATOR).json() == []


def test_api_maps_refusals_to_4xx(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:
        missing = tc.post("/providers/ghost/key", json={"api_key": "k"}, headers=OPERATOR)
        assert missing.status_code == 404
        assert tc.get("/providers/ghost/models", headers=OPERATOR).status_code == 404
        assert tc.delete("/providers/ghost", headers=OPERATOR).status_code == 404
        no_key = tc.post(
            "/providers", json={"name": "openai", "preset": "openai"}, headers=OPERATOR
        )
        assert no_key.status_code == 422 and "needs an API key" in no_key.text
        # A key in the query string is not how keys travel: the body is required.
        in_query = tc.post(
            "/providers", params={"name": "o", "preset": "openai", "api_key": "k"}, headers=OPERATOR
        )
        assert in_query.status_code == 422
        ghost = tc.post("/providers/ghost/extra-body", json={"extra_body": {}}, headers=OPERATOR)
        assert ghost.status_code == 404
        tc.post("/providers", json={"name": "o", "preset": "ollama"}, headers=OPERATOR)
        vendor_kwargs = tc.post(
            "/providers/o/extra-body",
            json={"extra_body": {"chat_template_kwargs": {}}},
            headers=OPERATOR,
        )
        assert vendor_kwargs.status_code == 422 and "chat_template_kwargs" in vendor_kwargs.text
        not_object = tc.post("/providers/o/extra-body", json={"extra_body": [1]}, headers=OPERATOR)
        assert not_object.status_code == 422


def test_the_capture_key_cannot_reach_any_provider_route(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    calls = [
        ("GET", "/providers", None),
        ("GET", "/providers/presets", None),
        ("GET", "/providers/discover", None),
        ("GET", "/providers/x/models", None),
        ("POST", "/providers", {"name": "x", "preset": "ollama"}),
        ("POST", "/providers/x/key", {"api_key": "k"}),
        ("POST", "/providers/x/rename", {"new_name": "y"}),
        ("POST", "/providers/x/extra-body", {"extra_body": {}}),
        ("DELETE", "/providers/x", None),
    ]
    with TestClient(create_app(api_settings())) as tc:
        for method, path, body in calls:
            resp = tc.request(method, path, json=body, headers=CAPTURE)
            assert resp.status_code == 403, (method, path, resp.status_code)
        assert tc.get("/providers", headers=OPERATOR).status_code == 200


# --- MCP and CLI parity -----------------------------------------------------------------------


def test_providers_are_identical_through_every_door(clean_db: None, monkeypatch):
    import asyncio
    import json

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

    # CLI: the key comes from stdin, so it never sits in argv or shell history.
    added = runner.invoke(
        cli.app,
        ["provider", "add", "openrouter", "--preset", "openrouter", "--key-stdin"],
        input=OPENROUTER_KEY + "\n",
    )
    assert added.exit_code == 0, added.output
    assert OPENROUTER_KEY not in added.output
    # A local Provider needs no key and asks for none.
    local = runner.invoke(cli.app, ["provider", "add", "ollama", "--preset", "ollama"])
    assert local.exit_code == 0, local.output

    async def via_registry_and_mcp() -> tuple[list, list, dict]:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            registry = build_registry(db, providers=desk(db))
            direct = await registry.get("providers").handler()
            async with Client(build_mcp(registry)) as mcp_client:
                tools = {t.name for t in (await mcp_client.list_tools()).tools}
                assert {"get_providers", "add_provider", "set_provider_key"} <= tools
                listed = await mcp_client.call_tool("get_providers", {})
                rotated = await mcp_client.call_tool(
                    "set_provider_key", {"name": "ollama", "api_key": "lan-key-7777"}
                )
            return direct, listed.structured_content["result"], rotated.structured_content
        finally:
            await db.close()

    direct, via_mcp, rotated = asyncio.run(via_registry_and_mcp())

    # Extra request fields: CLI (a JSON argument) -> API -> service; MCP takes an object.
    extra = runner.invoke(
        cli.app, ["provider", "set-extra-body", "openrouter", '{"reasoning":{"enabled":false}}']
    )
    assert extra.exit_code == 0, extra.output
    assert json.loads(extra.output)["extra_body"] == {"reasoning": {"enabled": False}}
    bad = runner.invoke(cli.app, ["provider", "set-extra-body", "openrouter", "[1]"])
    assert bad.exit_code != 0

    async def extra_via_mcp() -> dict:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            async with Client(build_mcp(build_registry(db, providers=desk(db)))) as mcp_client:
                done = await mcp_client.call_tool(
                    "set_provider_extra_body",
                    {"name": "ollama", "extra_body": {"keep_alive": "5m"}},
                )
            return done.structured_content
        finally:
            await db.close()

    assert asyncio.run(extra_via_mcp())["extra_body"] == {"keep_alive": "5m"}
    assert "7777" in json.dumps(rotated) and "lan-key" not in json.dumps(rotated)

    with TestClient(create_app(settings)) as tc:
        via_api = tc.get("/providers").json()
    listed = runner.invoke(cli.app, ["provider", "list"])
    assert listed.exit_code == 0, listed.output
    via_cli = json.loads(listed.output)

    assert [p["name"] for p in direct] == ["ollama", "openrouter"]
    assert via_api == via_cli
    assert direct == via_mcp
    assert via_api[0]["key"] == "••••7777"
    assert {p["name"]: p["extra_body"] for p in via_api} == {
        "ollama": {"keep_alive": "5m"},
        "openrouter": {"reasoning": {"enabled": False}},
    }


def test_cli_prompts_for_a_hosted_key_without_echoing_it(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app

    settings = Settings(auth_disabled=True, secrets_key=SECRETS_KEY)
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(
        cli.app, ["provider", "add", "groq", "--preset", "groq"], input="gsk-hidden-1234\n"
    )
    assert result.exit_code == 0, result.output
    assert "gsk-hidden" not in result.output and "••••1234" in result.output
