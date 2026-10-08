"""Per-Role daily Spend caps (#108): no Role spends past its cap, and the operator sees a
Chain's cost before saving it. Caps live apart from Profiles, so a switch never lifts
one. At the cap a Role's paid models are skipped for the UTC day and a free one later in
its Chain serves. Seams: the shared LLMRegistry, LoopRunner.tick, ProfileDesk, the HTTP
API (with auth), and MCP + CLI parity. Model servers are mocked at the transport.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.test_profiles import CRITIC_OK, DECIDE_OK, SECRETS_KEY, ModelServer, runner_on
from trader.llm.caps import SpendCaps
from trader.llm.profiles import ProfileError
from trader.llm.registry import LLMRegistry
from trader.llm.usage import UsageMeter
from trader.store.db import Database
from trader.store.repos import UsageRepo

TODAY = datetime.now(tz=UTC).date()
PAID = {"provider": "openai", "model": "gpt-5"}
FREE = {"provider": "spark", "model": "qwen3:8b"}
MESSAGES = [{"role": "user", "content": "decide"}]


async def spend(db: Database, role: str, usd: float, *, day=TODAY, tokens=(0, 0)) -> None:
    await UsageRepo(db).record(
        day=day,
        role=role,
        provider="openai",
        model="gpt-5",
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        cost_usd=usd,
    )


async def setup(db: Database, server: ModelServer, **caps_kwargs):
    """A paid OpenAI model ($1 / $1 per 1M by override) and a free local one."""
    from trader.llm.profiles import ProfileDesk
    from trader.llm.providers import ProviderDesk
    from trader.llm.registry import ROLES

    caps = SpendCaps(db, **caps_kwargs)
    offline = httpx.AsyncClient(transport=httpx.MockTransport(server))
    providers = ProviderDesk(db, secrets_key=SECRETS_KEY, http=offline)
    profiles = ProfileDesk(db, providers, caps=caps, transport=httpx.MockTransport(server))
    await providers.add(name="spark", preset="ollama", base_url="http://spark:11434/v1")
    await providers.add(name="openai", preset="openai", api_key="sk-key-12345")
    await providers.set_price("openai", "gpt-5", price_in=1.0, price_out=1.0)
    await profiles.create("trial")
    env = {role: [] for role in ROLES}
    registry = LLMRegistry(env, UsageMeter(UsageRepo(db)), profiles=profiles, caps=caps)
    return providers, profiles, caps, registry


def paid_and_free_server() -> ModelServer:
    return ModelServer({("api.openai.com", "gpt-5"): DECIDE_OK, ("spark", "qwen3:8b"): DECIDE_OK})


# --- the shared LLMRegistry -----------------------------------------------------------------


async def test_at_the_cap_paid_models_are_skipped_and_a_free_one_serves(db: Database):
    _, profiles, caps, registry = await setup(db, paid_and_free_server())
    await profiles.set_chain("trial", "decide", [PAID, FREE])
    await caps.set("decide", 0.5)

    below = await registry.complete("decide", MESSAGES)
    await spend(db, "decide", 0.6)
    at_cap = await registry.complete("decide", MESSAGES)
    again = await registry.complete("decide", MESSAGES)

    assert below.provider == "openai"
    assert (at_cap.provider, again.provider) == ("spark", "spark")
    hits = await db.fetch_all(
        "SELECT subject, payload FROM audit_events WHERE action = 'spend_cap_reached'"
    )
    assert [h["subject"] for h in hits] == ["decide"]  # once a day, not once a call
    assert hits[0]["payload"]["cap_usd"] == 0.5


async def test_a_profile_switch_keeps_the_cap(db: Database):
    _, profiles, caps, registry = await setup(db, paid_and_free_server())
    await profiles.set_chain("trial", "decide", [PAID, FREE])
    await profiles.duplicate("trial", "other")
    await caps.set("decide", 0.5)
    await spend(db, "decide", 0.6)

    await profiles.activate("other")
    served = await registry.complete("decide", MESSAGES)

    assert served.provider == "spark"
    assert (await caps.list())["decide"]["cap_usd"] == 0.5


async def test_the_critic_cap_defaults_to_the_desk_budget(db: Database):
    server = ModelServer({("api.openai.com", "gpt-5"): CRITIC_OK, ("spark", "qwen3:8b"): CRITIC_OK})
    _, profiles, caps, registry = await setup(db, server, defaults={"critic": 1.0})
    await profiles.set_chain("trial", "critic", [PAID, FREE])
    await spend(db, "critic", 1.0)

    listed = await caps.list()
    served = await registry.complete("critic", MESSAGES)

    assert listed["critic"] == {"cap_usd": 1.0, "source": "default", "spent_today": 1.0}
    assert listed["decide"] == {"cap_usd": None, "source": None, "spent_today": 0.0}
    assert served.provider == "spark"


async def test_with_nothing_free_left_the_role_fails_like_an_outage(db: Database):
    from trader.llm.base import LLMError

    _, profiles, caps, registry = await setup(db, paid_and_free_server())
    await profiles.set_chain("trial", "decide", [PAID])
    await caps.set("decide", 0.5)
    await spend(db, "decide", 0.6)

    with pytest.raises(LLMError, match="decide is at its daily spend cap"):
        await registry.complete("decide", MESSAGES)


# --- LoopRunner.tick ------------------------------------------------------------------------


async def test_the_wakeup_completes_on_the_local_fallback(db: Database):
    from tests.test_loop import NOW
    from trader.loop.journal import Journal

    _, profiles, caps, registry = await setup(db, paid_and_free_server())
    await profiles.set_chain("trial", "decide", [PAID, FREE])
    await caps.set("decide", 0.5)
    await spend(db, "decide", 0.6)

    results = await runner_on(db, registry).tick(NOW)
    row = await Journal(db).get(results[0].decision_id)

    assert (row["status"], row["served_by"]) == ("shadow", "spark:qwen3:8b")


async def test_with_no_fallback_the_wakeup_journals_an_error_and_the_loop_ticks_on(db: Database):
    from tests.test_loop import NOW
    from trader.loop.decider import DeciderError
    from trader.loop.journal import ERROR, Journal

    _, profiles, caps, registry = await setup(db, paid_and_free_server())
    await profiles.set_chain("trial", "decide", [PAID])
    await caps.set("decide", 0.5)
    await spend(db, "decide", 0.6)

    with pytest.raises(DeciderError) as raised:
        await runner_on(db, registry).tick(NOW)
    assert raised.value.transient  # run_forever backs off and keeps ticking
    row = (await Journal(db).recent(1))[0]
    assert row["status"] == ERROR and "spend cap" in row["raw_output"]

    await caps.set("decide", 5.0)  # the operator raises it; the next wakeup decides
    results = await runner_on(db, registry).tick(NOW + timedelta(hours=1))
    assert results[0].status == "shadow"


# --- ProfileDesk ----------------------------------------------------------------------------


async def test_a_save_returns_the_estimate_from_seven_day_volume(db: Database):
    _, profiles, caps, _ = await setup(db, paid_and_free_server())
    for days_ago in range(7):  # 1M prompt + 100k completion tokens a day
        await spend(
            db, "decide", 0.0, day=TODAY - timedelta(days=days_ago), tokens=(1_000_000, 100_000)
        )
    await spend(db, "decide", 0.0, day=TODAY - timedelta(days=8), tokens=(50_000_000, 0))  # too old

    saved = await profiles.set_chain("trial", "decide", [PAID, FREE])

    # 1M × $1 + 0.1M × $1 per 1M tokens, per day, priced at the first model.
    assert saved["estimate"] == {
        "usd_per_day": pytest.approx(1.1),
        "basis": "7-day average volume × openai:gpt-5",
    }


async def test_an_unpriced_paid_model_is_refused_under_a_cap(db: Database):
    server = ModelServer({("api.openai.com", "gpt-9-preview"): DECIDE_OK})
    _, profiles, caps, _ = await setup(db, server)
    unpriced = [{"provider": "openai", "model": "gpt-9-preview"}]

    await profiles.set_chain("trial", "decide", unpriced)  # no cap: allowed
    await caps.set("decide", 5.0)
    with pytest.raises(ProfileError, match="openai:gpt-9-preview has no price"):
        await profiles.set_chain("trial", "decide", unpriced)


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
        desk_critic_daily_usd=1.5,
    )


def test_api_sets_lists_and_clears_caps(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:
        assert tc.get("/caps", headers=OPERATOR).json()["critic"]["cap_usd"] == 1.5
        set_ = tc.put("/caps/decide", json={"daily_usd": 3}, headers=OPERATOR)
        assert set_.status_code == 200, set_.text
        assert set_.json()["decide"]["cap_usd"] == 3.0
        assert tc.put("/caps/decide", json={"daily_usd": -1}, headers=OPERATOR).status_code == 422
        assert tc.put("/caps/nope", json={"daily_usd": 1}, headers=OPERATOR).status_code == 422
        cleared = tc.delete("/caps/decide", headers=OPERATOR).json()
        assert cleared["decide"]["cap_usd"] is None
        audit = tc.get("/audit", headers=OPERATOR).json()
        caps_trail = [
            (e["action"], e["payload"]["surface"]) for e in audit if e["category"] == "caps"
        ]
        assert caps_trail == [("clear_spend_cap", "api"), ("set_spend_cap", "api")]


def test_the_capture_key_cannot_reach_cap_routes(clean_db: None):
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app(api_settings())) as tc:
        assert tc.get("/caps", headers=CAPTURE).status_code == 403
        assert tc.put("/caps/decide", json={"daily_usd": 1}, headers=CAPTURE).status_code == 403
        assert tc.delete("/caps/decide", headers=CAPTURE).status_code == 403


# --- MCP and CLI parity -----------------------------------------------------------------------


def test_caps_are_identical_through_every_door(clean_db: None, monkeypatch):
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
    set_ = runner.invoke(cli.app, ["cap", "set", "decide", "2.5"])
    assert set_.exit_code == 0, set_.output

    async def via_mcp():
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            registry = build_registry(db, caps=SpendCaps(db, defaults={"critic": 1.0}))
            async with Client(build_mcp(registry)) as mcp_client:
                tools = {t.name for t in (await mcp_client.list_tools()).tools}
                assert {"get_spend_caps", "set_spend_cap", "clear_spend_cap"} <= tools
                listed = await mcp_client.call_tool("get_spend_caps", {})
            return listed.structured_content
        finally:
            await db.close()

    via_mcp_caps = asyncio.run(via_mcp())
    via_cli = json.loads(runner.invoke(cli.app, ["cap", "list"]).output)
    assert via_cli == via_mcp_caps
    assert via_cli["decide"]["cap_usd"] == 2.5
    cleared = runner.invoke(cli.app, ["cap", "clear", "decide"])
    assert cleared.exit_code == 0 and json.loads(cleared.output)["decide"]["cap_usd"] is None
