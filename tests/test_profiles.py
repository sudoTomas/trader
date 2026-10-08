"""Profiles, Chains and the Probe (#107). The active Profile serves every Role through its
Chain, switched without a restart and only after a Probe. Seams: ProfileDesk, the shared
LLMRegistry, LoopRunner.tick, the HTTP API (with the auth middleware), and MCP + CLI
parity. Model servers are mocked at the transport.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from trader.llm.profiles import ProfileDesk, ProfileError
from trader.llm.providers import ProviderDesk
from trader.store.db import Database

SECRETS_KEY = Fernet.generate_key().decode()

DECIDE_OK = json.dumps(
    {
        "action": "hold",
        "conviction": 0.2,
        "rationale": "probe",
        "side": None,
        "notional_usd": None,
    }
)
ANALYST_OK = json.dumps({"stance": "neutral", "confidence": 0.5, "note": "probe"})
CRITIC_OK = json.dumps({"verdict": "approve", "notional_usd": None, "reason": "probe"})


class ModelServer:
    """Every Provider's HTTP, by host: ``replies[(host, model)]`` is a reply text or an
    HTTP status. Records ``(host, model, body)`` per chat call."""

    def __init__(self, replies: dict[tuple[str, str], str | int]) -> None:
        self.replies = dict(replies)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        host = request.url.host
        model = body.get("model", "")
        self.calls.append((host, model, body))
        reply = self.replies.get((host, model), 404)
        if isinstance(reply, int):
            return httpx.Response(reply, text="body that may echo a key")
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={"output": [], "usage": {}})
        if "systemone" in request.url.path:
            return httpx.Response(200, json={"answers": json.loads(reply), "usage": {}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    def models(self, host: str) -> list[str]:
        return [m for (h, m, _) in self.calls if h == host]


async def desks(
    db: Database, server: ModelServer, *, nemotron_base_url: str = ""
) -> tuple[ProviderDesk, ProfileDesk]:
    # The mock answers the price catalog with a 404 too: tests never leave the machine.
    offline = httpx.AsyncClient(transport=httpx.MockTransport(server))
    providers = ProviderDesk(db, secrets_key=SECRETS_KEY, http=offline)
    profiles = ProfileDesk(
        db,
        providers,
        nemotron_base_url=nemotron_base_url,
        transport=httpx.MockTransport(server),
    )
    return providers, profiles


async def with_local(db: Database, server: ModelServer, **kwargs):
    providers, profiles = await desks(db, server, **kwargs)
    await providers.add(name="spark", preset="ollama", base_url="http://spark:11434/v1")
    await providers.add(name="box", preset="vllm", base_url="http://box:8000/v1")
    return providers, profiles


# --- ProfileDesk ----------------------------------------------------------------------------


async def test_a_chain_is_saved_after_its_probe_and_names_its_models(db: Database):
    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK})
    _, profiles = await with_local(db, server)
    await profiles.create("trial")

    saved = await profiles.set_chain(
        "trial", "decide", [{"provider": "spark", "model": "qwen3:8b"}]
    )

    assert saved["profile"]["version"] == 2
    assert saved["profile"]["roles"] == {"decide": [{"provider": "spark", "model": "qwen3:8b"}]}
    assert [(p["provider"], p["model"], p["ok"]) for p in saved["probe"]] == [
        ("spark", "qwen3:8b", True)
    ]
    assert saved["warnings"] == []
    assert server.models("spark") == ["qwen3:8b"]


async def test_a_first_entry_failing_its_probe_saves_nothing(db: Database):
    # Kev-as-decider, generalized: the model answers, but not a valid decide.
    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK, ("box", "tiny"): '{"action": "maybe"}'})
    _, profiles = await with_local(db, server)
    await profiles.create("trial")
    await profiles.set_chain("trial", "decide", [{"provider": "spark", "model": "qwen3:8b"}])

    with pytest.raises(ProfileError, match=r"box:tiny failed its decide Probe: .*contract"):
        await profiles.set_chain("trial", "decide", [{"provider": "box", "model": "tiny"}])

    after = (await profiles.list())[0]
    assert (after["version"], after["roles"]["decide"][0]["model"]) == (2, "qwen3:8b")


async def test_a_failing_fallback_saves_with_a_warning(db: Database):
    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK, ("box", "gone"): 503})
    _, profiles = await with_local(db, server)
    await profiles.create("trial")

    saved = await profiles.set_chain(
        "trial",
        "decide",
        [{"provider": "spark", "model": "qwen3:8b"}, {"provider": "box", "model": "gone"}],
    )

    assert saved["profile"]["roles"]["decide"][1] == {"provider": "box", "model": "gone"}
    assert saved["warnings"] == ["decide fallback box:gone failed its Probe: HTTP 503"]
    assert "echo" not in json.dumps(saved)  # status text only, never a response body


async def test_roles_refuse_providers_that_cannot_serve_them(db: Database):
    server = ModelServer({})
    providers, profiles = await with_local(db, server)
    await providers.add(name="kev", preset="jev", api_key="kev-key-12345")
    await profiles.create("trial")

    refusals = {
        "sentiment": ("spark", "x_search, which only xAI serves"),
        "confirm": ("spark", "Jev Provider only"),
        "decide": ("kev", "answers question sets only"),
        "brief": ("kev", "answers question sets only"),
    }
    for role, (provider, reason) in refusals.items():
        with pytest.raises(ProfileError, match=reason):
            await profiles.set_chain("trial", role, [{"provider": provider, "model": "m"}])
    assert server.calls == []  # refused before any Probe
    assert (await profiles.list())[0]["version"] == 1


async def test_confirm_can_be_switched_off_with_an_empty_chain(db: Database):
    _, profiles = await with_local(db, ModelServer({}))
    await profiles.create("trial")
    saved = await profiles.set_chain("trial", "confirm", [])
    assert saved["profile"]["roles"] == {"confirm": []}
    with pytest.raises(ProfileError, match="decide needs at least one model"):
        await profiles.set_chain("trial", "decide", [])


async def test_revert_restores_the_earlier_chains(db: Database):
    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK, ("box", "llama"): DECIDE_OK})
    _, profiles = await with_local(db, server)
    await profiles.create("trial")
    await profiles.set_chain("trial", "decide", [{"provider": "spark", "model": "qwen3:8b"}])
    await profiles.set_chain("trial", "decide", [{"provider": "box", "model": "llama"}])

    reverted = await profiles.revert("trial", 2)

    assert reverted["profile"]["version"] == 2
    assert reverted["profile"]["roles"]["decide"] == [{"provider": "spark", "model": "qwen3:8b"}]
    history = await profiles.get("trial")
    assert [v["version"] for v in history["versions"]] == [3, 2, 1]  # nothing is rewritten


async def test_one_profile_is_active_and_switching_probes_first(db: Database):
    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK})
    _, profiles = await with_local(db, server)
    first = await profiles.create("everyday")
    second = await profiles.duplicate("everyday", "spicy")
    assert (first["active"], second["active"]) == (True, False)
    await profiles.set_chain("spicy", "decide", [{"provider": "spark", "model": "qwen3:8b"}])
    server.replies[("spark", "qwen3:8b")] = 500  # the model went away since the save

    with pytest.raises(ProfileError, match="failed its decide Probe: HTTP 500"):
        await profiles.activate("spicy")
    assert [p["name"] for p in await profiles.list() if p["active"]] == ["everyday"]

    server.replies[("spark", "qwen3:8b")] = DECIDE_OK
    switched = await profiles.activate("spicy")
    assert switched["profile"]["active"] is True
    assert [p["name"] for p in await profiles.list() if p["active"]] == ["spicy"]


async def test_rename_and_reset_keep_the_profile_and_its_history(db: Database):
    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK})
    _, profiles = await with_local(db, server)
    await profiles.create("trial")
    await profiles.set_chain("trial", "decide", [{"provider": "spark", "model": "qwen3:8b"}])

    renamed = await profiles.rename("trial", "nemo-vs-qwen")
    reset = await profiles.reset_role("nemo-vs-qwen", "decide")

    assert (renamed["name"], renamed["version"]) == ("nemo-vs-qwen", 2)
    assert (reset["version"], reset["roles"]) == (3, {})
    assert (await profiles.routes())["decide"] == {"source": ".env"}


async def test_every_change_is_audited_with_caller_and_surface(db: Database):
    from trader.store.audit import CALLER, SURFACE

    _, profiles = await with_local(db, ModelServer({}))
    caller, surface = CALLER.set("operator"), SURFACE.set("cli")
    try:
        await profiles.create("trial")
        await profiles.rename("trial", "trial-2")
    finally:
        CALLER.reset(caller)
        SURFACE.reset(surface)

    rows = await db.fetch_all(
        "SELECT actor, action, subject, payload FROM audit_events"
        " WHERE category = 'profiles' ORDER BY id"
    )
    assert [(r["actor"], r["action"], r["subject"], r["payload"]["surface"]) for r in rows] == [
        ("operator", "create_profile", "trial", "cli"),
        ("operator", "rename_profile", "trial-2", "cli"),
    ]


async def test_the_nemotron_guard_follows_the_url_and_nothing_else(db: Database):
    server = ModelServer({("spark", "nemotron"): DECIDE_OK, ("box", "llama"): DECIDE_OK})
    _, profiles = await with_local(db, server, nemotron_base_url="http://spark:11434/v1/")
    await profiles.create("trial")
    await profiles.set_chain(
        "trial",
        "decide",
        [{"provider": "spark", "model": "nemotron"}, {"provider": "box", "model": "llama"}],
    )

    bodies = {host: body for host, _, body in server.calls}
    assert bodies["spark"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in bodies["box"]


async def test_a_provider_in_a_current_chain_cannot_be_removed(db: Database):
    from trader.llm.providers import ProviderError

    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK})
    providers, profiles = await with_local(db, server)
    await profiles.create("trial")
    await profiles.set_chain("trial", "decide", [{"provider": "spark", "model": "qwen3:8b"}])

    with pytest.raises(ProviderError, match="trial uses it .decide.; change that Chain first"):
        await providers.remove("spark")
    await providers.remove("box")  # referenced by nothing
    await profiles.reset_role("trial", "decide")
    await providers.remove("spark")  # only an old version names it now
    assert await providers.list() == []


# --- the shared LLMRegistry -----------------------------------------------------------------


class EnvClient:
    """Today's .env wiring for one Role, as a client that records being called."""

    def __init__(self, name: str, text: str) -> None:
        self.name, self.model, self.text, self.calls = name, f"{name}-model", text, 0

    async def complete(self, messages, *, schema=None, max_tokens=1024, temperature=0.2):
        from trader.llm.base import Completion, Usage

        self.calls += 1
        return Completion(self.text, Usage(1, 1), 0.0, self.name, self.model)


def env_registry(profiles: ProfileDesk, meter=None):
    from trader.llm.registry import ROLES, LLMRegistry

    env = {role: [EnvClient(f"env-{role}", DECIDE_OK)] for role in ROLES}
    return env, LLMRegistry(env, meter, profiles=profiles)


async def test_with_no_profile_every_role_routes_as_today(db: Database):
    from trader.llm.registry import ROLES

    _, profiles = await with_local(db, ModelServer({}))
    env, registry = env_registry(profiles)

    for role in ROLES:
        completion = await registry.complete(role, [{"role": "user", "content": "hi"}])
        assert (completion.provider, completion.profile_version) == (f"env-{role}", None)
        assert env[role][0].calls == 1


async def test_switching_the_active_profile_reaches_the_next_call(db: Database):
    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK})
    _, profiles = await with_local(db, server)
    env, registry = env_registry(profiles)
    await profiles.create("everyday")
    await profiles.create("spicy")
    await profiles.set_chain("spicy", "decide", [{"provider": "spark", "model": "qwen3:8b"}])
    messages = [{"role": "user", "content": "decide"}]

    before = await registry.complete("decide", messages)
    switched = await profiles.activate("spicy")
    after = await registry.complete("decide", messages)
    analyst = await registry.complete("analyst", messages)

    assert before.provider == "env-decide"
    assert (after.provider, after.model) == ("spark", "qwen3:8b")
    assert after.profile_version is not None
    assert switched["profile"]["version"] == 2
    assert analyst.provider == "env-analyst"  # a Role the Profile leaves out keeps .env


async def test_a_failing_primary_falls_to_the_next_entry_and_is_metered(db: Database):
    from trader.llm.usage import UsageMeter
    from trader.store.repos import UsageRepo

    server = ModelServer({("spark", "qwen3:8b"): DECIDE_OK, ("box", "llama"): DECIDE_OK})
    _, profiles = await with_local(db, server)
    _, registry = env_registry(profiles, UsageMeter(UsageRepo(db)))
    await profiles.create("trial")
    await profiles.set_chain(
        "trial",
        "decide",
        [{"provider": "spark", "model": "qwen3:8b"}, {"provider": "box", "model": "llama"}],
    )
    server.replies[("spark", "qwen3:8b")] = 503

    completion = await registry.complete("decide", [{"role": "user", "content": "decide"}])

    assert (completion.provider, completion.model) == ("box", "llama")
    usage = await db.fetch_all("SELECT role, provider, model, requests FROM usage_daily")
    assert [(u["role"], u["provider"], u["model"], u["requests"]) for u in usage] == [
        ("decide", "box", "llama", 1)
    ]


async def test_confirm_switched_off_has_no_route(db: Database):
    from trader.llm.registry import NoRoute

    _, profiles = await with_local(db, ModelServer({}))
    _, registry = env_registry(profiles)
    await profiles.create("trial")
    await profiles.set_chain("trial", "confirm", [])

    with pytest.raises(NoRoute, match="confirm is off"):
        await registry.complete("confirm", [{"role": "user", "content": "x"}])


# --- LoopRunner.tick ------------------------------------------------------------------------

OPEN_REPLY = json.dumps(
    {
        "action": "open",
        "conviction": 0.8,
        "rationale": "momentum",
        "side": "buy",
        "notional_usd": 500,
    }
)


class SpyGuard:
    """The real RiskGuard, recording every argument it is handed."""

    def __init__(self, db: Database) -> None:
        from tests.test_loop import LIMITS
        from trader.risk.guard import RiskGuard

        self._guard = RiskGuard(db, LIMITS)
        self.seen: list[tuple[tuple, dict]] = []

    async def evaluate(self, *args, **kwargs):
        self.seen.append((args, kwargs))
        return await self._guard.evaluate(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._guard, name)


def runner_on(db: Database, registry, **kwargs):
    from tests.test_loop import BTC, StubMarketData
    from trader.loop.runner import LoopRunner

    return LoopRunner(
        db=db,
        market_data=StubMarketData(),
        llm=registry,
        guard=kwargs.pop("guard", None) or SpyGuard(db),
        symbols=[BTC],
        shadow=True,
        equity=100_000.0,
        **kwargs,
    )


async def test_the_journal_names_the_profile_version_and_the_serving_model(db: Database):
    from tests.test_loop import NOW
    from trader.loop.journal import Journal

    server = ModelServer({("spark", "qwen3:8b"): OPEN_REPLY, ("box", "llama"): OPEN_REPLY})
    _, profiles = await with_local(db, server)
    _, registry = env_registry(profiles)

    async def wakeup() -> dict[str, Any]:
        # A fresh runner per wakeup: one heartbeat each, on the same shared registry.
        results = await runner_on(db, registry).tick(NOW)
        return await Journal(db).get(results[0].decision_id)

    env_row = await wakeup()
    await profiles.create("trial")
    saved = await profiles.set_chain(
        "trial",
        "decide",
        [{"provider": "spark", "model": "qwen3:8b"}, {"provider": "box", "model": "llama"}],
    )
    version_id = (await db.fetch_one("SELECT current_version FROM llm_profiles"))["current_version"]
    primary_row = await wakeup()
    server.replies[("spark", "qwen3:8b")] = 503
    fallback_row = await wakeup()

    assert saved["profile"]["version"] == 2
    assert (env_row["profile_version"], env_row["served_by"]) == (
        None,
        "env-decide:env-decide-model",
    )
    assert (primary_row["profile_version"], primary_row["served_by"]) == (
        version_id,
        "spark:qwen3:8b",
    )
    assert (fallback_row["profile_version"], fallback_row["served_by"]) == (version_id, "box:llama")


async def test_riskguard_and_the_executor_see_exactly_what_they_did_before(db: Database):
    """The power ceiling of this slice: which model answered changes the journal's
    attribution and nothing downstream of the decide."""
    from tests.test_loop import NOW

    server = ModelServer({("spark", "qwen3:8b"): OPEN_REPLY})
    _, profiles = await with_local(db, server)
    env, registry = env_registry(profiles)
    env["decide"][0].text = OPEN_REPLY

    on_env = SpyGuard(db)
    await runner_on(db, registry, guard=on_env).tick(NOW)
    await profiles.create("trial")
    await profiles.set_chain("trial", "decide", [{"provider": "spark", "model": "qwen3:8b"}])
    on_profile = SpyGuard(db)
    await runner_on(db, registry, guard=on_profile).tick(NOW)

    assert on_profile.seen == on_env.seen
    (intent, *_), _ = on_profile.seen[0]
    # Structurally: the intent RiskGuard judges has no field that could carry a model or
    # Profile, so no Chain can reach past the decide.
    assert {f.name for f in fields(intent)} == {
        "symbol",
        "side",
        "qty",
        "notional",
        "order_type",
        "limit_price",
        "time_in_force",
    }


async def test_confirm_switched_off_in_the_profile_journals_no_column(db: Database):
    from tests.test_loop import NOW
    from trader.loop.confirm import Confirmer
    from trader.loop.journal import Journal

    _, profiles = await with_local(db, ModelServer({}))
    env, registry = env_registry(profiles)
    env["decide"][0].text = OPEN_REPLY
    await profiles.create("trial")
    await profiles.set_chain("trial", "confirm", [])

    results = await runner_on(db, registry, confirm=Confirmer(registry)).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)
    assert (row["confirm"], row["decision"]["action"]) == (None, "open")


async def test_a_sentiment_chain_serves_x_search_from_its_xai_provider(db: Database):
    from tests.test_loop import BTC
    from trader.research.sentiment import SentimentService

    server = ModelServer(
        {("api.x.ai", "grok-4.6"): '{"score": 0.1, "volume": "low", "drivers": []}'}
    )
    providers, profiles = await with_local(db, server)
    await providers.add(name="grok", preset="xai", api_key="xai-key-12345")
    await profiles.create("trial")
    await profiles.set_chain("trial", "sentiment", [{"provider": "grok", "model": "grok-4.6"}])
    service = SentimentService(db, None, profiles=profiles)

    await service.get_x_sentiment(BTC)

    polled = [(h, m) for h, m, body in server.calls if body.get("tools")]
    assert polled == [("api.x.ai", "grok-4.6"), ("api.x.ai", "grok-4.6")]  # Probe, then poll
    row = await db.fetch_one("SELECT model FROM sentiment_scores")
    assert row["model"] == "grok-4.6"


async def test_with_no_sentiment_model_a_poll_is_unavailable_and_writes_nothing(db: Database):
    from tests.test_loop import BTC
    from trader.research.sentiment import SentimentService

    _, profiles = await with_local(db, ModelServer({}))
    result = await SentimentService(db, None, profiles=profiles).get_x_sentiment(BTC)

    assert result == {"status": "unavailable", "symbol": "BTC/USD", "reason": "no sentiment model"}
    assert await db.fetch_all("SELECT * FROM sentiment_scores") == []


# --- the HTTP API and its auth ----------------------------------------------------------------

OPERATOR = {"X-API-Key": "operator-key"}
CAPTURE = {"X-API-Key": "capture-key"}
#: Nothing listens here, so a Probe against it fails fast and offline.
CLOSED = "http://127.0.0.1:9/v1"


def api_settings():
    from trader.ops.config import Settings

    return Settings(
        auth_disabled=False,
        api_key="operator-key",
        capture_api_key="capture-key",
        secrets_key=SECRETS_KEY,
    )


def test_api_runs_the_profile_lifecycle(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:

        def ok(resp):
            assert resp.status_code == 200, resp.text
            return resp.json()

        ok(
            tc.post(
                "/providers",
                json={"name": "lan", "preset": "vllm", "base_url": CLOSED},
                headers=OPERATOR,
            )
        )
        assert ok(tc.post("/profiles", json={"name": "everyday"}, headers=OPERATOR))["active"]
        ok(tc.post("/profiles/everyday/duplicate", json={"new_name": "trial"}, headers=OPERATOR))
        off = ok(tc.put("/profiles/trial/roles/confirm", json={"chain": []}, headers=OPERATOR))
        assert off["profile"]["roles"] == {"confirm": []}
        switched = ok(tc.post("/profiles/trial/activate", headers=OPERATOR))
        assert switched["profile"]["active"] is True
        routes = ok(tc.get("/roles", headers=OPERATOR))
        assert routes["confirm"]["source"] == "profile" and routes["decide"] == {"source": ".env"}
        ok(tc.post("/profiles/trial/rename", json={"new_name": "kev-off"}, headers=OPERATOR))
        reverted = ok(tc.post("/profiles/kev-off/revert", json={"version": 1}, headers=OPERATOR))
        assert reverted["profile"]["roles"] == {}
        ok(tc.put("/profiles/kev-off/roles/confirm", json={"chain": []}, headers=OPERATOR))
        assert ok(tc.delete("/profiles/kev-off/roles/confirm", headers=OPERATOR))["roles"] == {}
        shown = ok(tc.get("/profiles/kev-off", headers=OPERATOR))
        assert [v["version"] for v in shown["versions"]] == [4, 3, 2, 1]  # revert moved a pointer
        assert [p["name"] for p in ok(tc.get("/profiles", headers=OPERATOR))] == [
            "everyday",
            "kev-off",
        ]
        audit = ok(tc.get("/audit", headers=OPERATOR))
        trail = [e for e in audit if e["category"] == "profiles"]
        assert {(e["actor"], e["payload"]["surface"]) for e in trail} == {("operator", "api")}


def test_api_maps_refusals_to_4xx(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:
        tc.post(
            "/providers",
            json={"name": "lan", "preset": "vllm", "base_url": CLOSED},
            headers=OPERATOR,
        )
        tc.post("/profiles", json={"name": "trial"}, headers=OPERATOR)
        assert tc.get("/profiles/ghost", headers=OPERATOR).status_code == 404
        assert tc.post("/profiles/ghost/activate", headers=OPERATOR).status_code == 404
        unprobed = tc.put(
            "/profiles/trial/roles/decide",
            json={"chain": [{"provider": "lan", "model": "m"}]},
            headers=OPERATOR,
        )
        assert unprobed.status_code == 422
        assert "failed its decide Probe: unreachable (ConnectError)" in unprobed.text
        wrong_kind = tc.put(
            "/profiles/trial/roles/sentiment",
            json={"chain": [{"provider": "lan", "model": "m"}]},
            headers=OPERATOR,
        )
        assert wrong_kind.status_code == 422 and "only xAI" in wrong_kind.text
        assert tc.get("/profiles/trial", headers=OPERATOR).json()["version"] == 1


def test_the_capture_key_cannot_reach_any_profile_route(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    calls = [
        ("GET", "/profiles", None),
        ("GET", "/profiles/x", None),
        ("GET", "/roles", None),
        ("POST", "/profiles", {"name": "x"}),
        ("POST", "/profiles/x/rename", {"new_name": "y"}),
        ("POST", "/profiles/x/duplicate", {"new_name": "y"}),
        ("PUT", "/profiles/x/roles/confirm", {"chain": []}),
        ("DELETE", "/profiles/x/roles/confirm", None),
        ("POST", "/profiles/x/activate", None),
        ("POST", "/profiles/x/revert", {"version": 1}),
    ]
    with TestClient(create_app(api_settings())) as tc:
        for method, path, body in calls:
            resp = tc.request(method, path, json=body, headers=CAPTURE)
            assert resp.status_code == 403, (method, path, resp.status_code)
        assert tc.get("/profiles", headers=OPERATOR).status_code == 200


# --- MCP and CLI parity -----------------------------------------------------------------------


def test_profiles_are_identical_through_every_door(clean_db: None, monkeypatch):
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

    def cli_json(*args: str):
        result = runner.invoke(cli.app, ["profile", *args])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    cli_json("create", "everyday")
    cli_json("duplicate", "everyday", "trial")
    off = cli_json("set", "trial", "confirm")  # no --model: the Chain is empty, confirm off
    assert off["profile"]["roles"] == {"confirm": []}

    async def via_registry_and_mcp() -> tuple[list, list]:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            providers = ProviderDesk(db, secrets_key=SECRETS_KEY)
            registry = build_registry(db, providers=providers, profiles=ProfileDesk(db, providers))
            direct = await registry.get("profiles").handler()
            async with Client(build_mcp(registry)) as mcp_client:
                tools = {t.name for t in (await mcp_client.list_tools()).tools}
                assert {"get_profiles", "set_role_chain", "activate_profile"} <= tools
                listed = await mcp_client.call_tool("get_profiles", {})
                await mcp_client.call_tool("activate_profile", {"name": "trial"})
            return direct, listed.structured_content["result"]
        finally:
            await db.close()

    direct, via_mcp = asyncio.run(via_registry_and_mcp())
    with TestClient(create_app(settings)) as tc:
        via_api = tc.get("/profiles").json()
        audit = tc.get("/audit").json()
    via_cli = cli_json("list")

    assert [p["name"] for p in direct] == ["everyday", "trial"]
    assert direct == via_mcp
    assert via_api == via_cli
    assert [p["name"] for p in via_api if p["active"]] == ["trial"]  # MCP's switch stuck
    surfaces = [
        (e["action"], e["payload"]["surface"]) for e in audit if e["category"] == "profiles"
    ]
    assert surfaces == [
        ("activate_profile", "mcp"),
        ("set_role_chain", "cli"),
        ("duplicate_profile", "cli"),
        ("create_profile", "cli"),
    ]


def test_cli_reads_a_chain_as_provider_colon_model(monkeypatch):
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli

    sent: list[tuple[str, str, Any, str | None]] = []

    def api(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        sent.append(
            (request.method, request.url.path, body, request.headers.get("x-trader-surface"))
        )
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        cli,
        "_client",
        lambda: httpx.Client(base_url="http://api", transport=httpx.MockTransport(api)),
    )
    runner = CliRunner()
    chained = runner.invoke(
        cli.app,
        [
            "profile", "set", "trial", "decide",
            "--model", "spark:qwen3:8b",
            "--model", "openrouter:meta-llama/llama-3.3-70b:free",
        ],
    )  # fmt: skip
    off = runner.invoke(cli.app, ["profile", "set", "trial", "confirm"])
    bad = runner.invoke(cli.app, ["profile", "set", "trial", "decide", "--model", "qwen3"])

    assert (chained.exit_code, off.exit_code) == (0, 0)
    assert sent == [
        (
            "PUT",
            "/profiles/trial/roles/decide",
            {
                "chain": [
                    {"provider": "spark", "model": "qwen3:8b"},
                    {"provider": "openrouter", "model": "meta-llama/llama-3.3-70b:free"},
                ]
            },
            "cli",
        ),
        ("PUT", "/profiles/trial/roles/confirm", {"chain": []}, "cli"),
    ]
    assert bad.exit_code != 0 and "not provider:model" in bad.output
