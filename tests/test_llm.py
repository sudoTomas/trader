"""LLM layer tests — recorded fixtures, no live model calls (a Spark smoke runs
outside pytest). The usage test hits the real compose Postgres.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from trader.llm.base import Completion, LLMError, Usage
from trader.llm.nemotron import nemotron_client
from trader.llm.openai_compat import OpenAIChatClient
from trader.llm.registry import LLMRegistry, build_llm_registry
from trader.llm.usage import (
    UsageMeter,
    price_per_million,
    x_search_tool_cost_usd,
    x_search_units_from_citations,
)
from trader.ops.config import Settings
from trader.store.db import Database
from trader.store.repos import UsageRepo

SETTINGS = Settings(auth_disabled=True)

VLLM_RESPONSE = {
    "choices": [{"message": {"content": '{"action": "hold"}'}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 8},
}


def capture_client(seen: dict, response: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=response)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://spark")


async def test_nemotron_always_sends_enable_thinking_false():
    seen: dict = {}
    client = nemotron_client(SETTINGS, client=capture_client(seen, VLLM_RESPONSE))
    completion = await client.complete([{"role": "user", "content": "hi"}])

    assert seen["path"] == "/chat/completions"
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}  # the invariant
    assert seen["body"]["model"] == "nemotron-3-super"
    assert completion.text == '{"action": "hold"}'
    assert completion.usage == Usage(prompt_tokens=120, completion_tokens=8)
    assert completion.cost_usd == 0.0  # our own silicon


async def test_nemotron_schema_switches_to_json_schema_mode():
    seen: dict = {}
    client = nemotron_client(SETTINGS, client=capture_client(seen, VLLM_RESPONSE))
    schema = {"type": "object", "properties": {"action": {"type": "string"}}}
    await client.complete([{"role": "user", "content": "decide"}], schema=schema)

    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == schema
    # JSON mode must not displace the throughput flag.
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_price_table():
    assert price_per_million("nemotron", "nemotron-3-super") == (0.0, 0.0)
    # Live default is still grok-4; current lineup must not fail-loud if flipped.
    assert price_per_million("xai", "grok-4") == (3.00, 15.00)
    assert price_per_million("xai", "grok-4-mini") == (0.30, 0.50)
    assert price_per_million("xai", "grok-4.6") == (2.00, 6.00)
    assert price_per_million("xai", "grok-4.5") == (2.00, 6.00)
    assert price_per_million("xai", "grok-4.3") == (1.25, 2.50)
    assert price_per_million("xai", "grok-4.20-0309-reasoning") == (1.25, 2.50)
    assert price_per_million("xai", "grok-4.20-0309-non-reasoning") == (1.25, 2.50)
    assert price_per_million("xai", "grok-4.20-multi-agent-0309") == (1.25, 2.50)


def test_unknown_model_prices_at_configured_default_and_logs_once(monkeypatch):
    """Unknown models must not raise mid-loop. The table is the default; a miss uses
    the configured default (0 unless the caller passes the endpoint price) and logs once."""
    from trader.llm import usage as usage_mod

    logged = getattr(usage_mod, "_unknown_price_logged", None)
    if logged is not None:
        logged.clear()
    events: list[tuple] = []

    def capture(event, **kwargs):
        events.append((event, kwargs))

    monkeypatch.setattr(usage_mod.log, "warning", capture)

    assert price_per_million("xai", "grok-99") == (0.0, 0.0)
    assert price_per_million("xai", "grok-99") == (0.0, 0.0)  # second call stays quiet
    assert price_per_million("openai_compat", "gpt-4o-mini", default=(0.15, 0.60)) == (0.15, 0.60)
    # Non-zero override replaces a table hit (TRADER_DECIDE_PRICE_IN/OUT).
    assert price_per_million("xai", "grok-4", override=(1.0, 2.0)) == (1.0, 2.0)
    assert price_per_million("nemotron", "nemotron-3-super") == (0.0, 0.0)

    unknown = [kw for event, kw in events if event == "llm_price_unknown"]
    assert [kw["model"] for kw in unknown] == ["grok-99", "gpt-4o-mini"]
    assert unknown[0]["price_in"] == 0.0 and unknown[0]["price_out"] == 0.0
    assert unknown[1]["price_in"] == 0.15 and unknown[1]["price_out"] == 0.60


def test_x_search_tool_cost_flips_at_2026_09_21_noon_pt():
    from zoneinfo import ZoneInfo

    pt = ZoneInfo("America/Los_Angeles")
    before = datetime(2026, 9, 21, 11, 59, tzinfo=pt)
    at = datetime(2026, 9, 21, 12, 0, tzinfo=pt)
    # Until the cutover: $5 / 1k calls, post/profile counts ignored.
    assert x_search_tool_cost_usd(calls=1, posts=25, profiles=3, when=before) == 5.0 / 1000
    # From 2026-09-21 12:00 PT: $5 / 1k posts fetched + $10 / 1k profiles.
    assert x_search_tool_cost_usd(calls=1, posts=25, profiles=3, when=at) == pytest.approx(
        25 * 5.0 / 1000 + 3 * 10.0 / 1000
    )


def test_x_search_units_count_status_urls_as_posts():
    posts, profiles = x_search_units_from_citations(
        [
            "https://x.com/i/status/1",
            "https://x.com/foo/status/2",
            "https://x.com/elonmusk",
            "https://example.com/not-x",
        ]
    )
    assert (posts, profiles) == (2, 1)


class _StubClient:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.model = "stub-model"
        self._fail = fail
        self.calls = 0

    async def complete(self, messages, *, schema=None, max_tokens=1024, temperature=0.2):
        self.calls += 1
        if self._fail:
            raise LLMError(f"{self.name} down")
        return Completion(
            text="ok",
            usage=Usage(prompt_tokens=100, completion_tokens=10),
            cost_usd=0.05,
            provider=self.name,
            model=self.model,
        )


async def test_registry_falls_through_and_exhausts():
    primary, fallback = _StubClient("primary", fail=True), _StubClient("fallback")
    registry = LLMRegistry({"brief": [primary, fallback]}, meter=None)

    completion = await registry.complete("brief", [{"role": "user", "content": "x"}])
    assert completion.provider == "fallback"
    assert primary.calls == 1 and fallback.calls == 1

    dead = LLMRegistry({"brief": [_StubClient("a", fail=True)]}, meter=None)
    with pytest.raises(LLMError, match="all providers failed"):
        await dead.complete("brief", [])
    with pytest.raises(LLMError, match="no LLM provider configured"):
        await dead.complete("decide", [])


async def test_unknown_model_completion_meters_default_without_raising():
    from trader.llm import usage as usage_mod

    logged = getattr(usage_mod, "_unknown_price_logged", None)
    if logged is not None:
        logged.clear()
    client = OpenAIChatClient(
        name="openai_compat",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        client=capture_client({}, VLLM_RESPONSE),
    )
    first = await client.complete([{"role": "user", "content": "hi"}])
    second = await client.complete([{"role": "user", "content": "hi"}])
    assert first.cost_usd == 0.0
    assert second.cost_usd == 0.0
    assert len(usage_mod._unknown_price_logged) == 1


async def test_configured_price_is_what_an_unknown_model_costs():
    client = OpenAIChatClient(
        name="openai_compat",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        client=capture_client({}, VLLM_RESPONSE),
        price_in=3.0,
        price_out=15.0,
    )
    completion = await client.complete([{"role": "user", "content": "hi"}])
    assert completion.cost_usd == pytest.approx((120 * 3.0 + 8 * 15.0) / 1_000_000)


def test_decide_settings_read_the_trader_decide_env_names(monkeypatch):
    monkeypatch.setenv("TRADER_DECIDE_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("TRADER_DECIDE_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("TRADER_DECIDE_API_KEY", "gsk-test")
    monkeypatch.setenv("TRADER_DECIDE_EXTRA_BODY", '{"seed": 1}')
    monkeypatch.setenv("TRADER_DECIDE_PRICE_IN", "0.59")
    monkeypatch.setenv("TRADER_DECIDE_PRICE_OUT", "0.79")
    monkeypatch.setenv("TRADER_RESEARCH_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("TRADER_RESEARCH_MODEL", "llama3.2")
    monkeypatch.setenv("TRADER_RESEARCH_API_KEY", "")
    monkeypatch.setenv("TRADER_RESEARCH_EXTRA_BODY", "{}")
    monkeypatch.setenv("TRADER_RESEARCH_PRICE_IN", "0")
    monkeypatch.setenv("TRADER_RESEARCH_PRICE_OUT", "0")
    settings = Settings(auth_disabled=True)
    assert settings.decide_base_url == "https://api.groq.com/openai/v1"
    assert settings.decide_model == "llama-3.3-70b-versatile"
    assert settings.decide_api_key == "gsk-test"
    assert settings.decide_extra_body == '{"seed": 1}'
    assert settings.decide_price_in == pytest.approx(0.59)
    assert settings.decide_price_out == pytest.approx(0.79)
    assert settings.research_base_url == "http://localhost:11434/v1"
    assert settings.research_model == "llama3.2"
    assert settings.research_price_in == 0.0 and settings.research_price_out == 0.0


def test_decide_extra_body_must_be_a_json_object():
    with pytest.raises(ValidationError):
        Settings(auth_disabled=True, decide_extra_body="[]")
    with pytest.raises(ValidationError):
        Settings(auth_disabled=True, research_extra_body="not-json")
    blank = Settings(auth_disabled=True, decide_extra_body="  ")
    assert blank.decide_extra_body == ""


async def test_generic_decide_omits_enable_thinking_and_keeps_json_schema():
    seen: dict = {}
    settings = Settings(
        auth_disabled=True,
        decide_base_url="https://api.openai.com/v1",
        decide_model="gpt-4o-mini",
        decide_api_key="sk-test",
        decide_extra_body='{"seed": 7}',
    )
    from trader.llm.registry import build_decide_client

    client = build_decide_client(settings, client=capture_client(seen, VLLM_RESPONSE))
    schema = {"type": "object", "properties": {"action": {"type": "string"}}}
    await client.complete([{"role": "user", "content": "decide"}], schema=schema)

    assert seen["body"]["model"] == "gpt-4o-mini"
    assert "chat_template_kwargs" not in seen["body"]  # OpenAI rejects this argument
    assert seen["body"]["seed"] == 7
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert seen["body"]["response_format"]["json_schema"]["schema"] == schema
    assert seen["body"]["response_format"]["json_schema"]["strict"] is True


async def test_decide_url_matching_nemotron_preset_keeps_thinking_disabled():
    seen: dict = {}
    settings = Settings(
        auth_disabled=True,
        decide_base_url=SETTINGS.nemotron_base_url,
        decide_model="nemotron-3-super",
        decide_extra_body='{"chat_template_kwargs": {"enable_thinking": true, "keep": 1}}',
    )
    from trader.llm.registry import build_decide_client

    client = build_decide_client(settings, client=capture_client(seen, VLLM_RESPONSE))
    await client.complete([{"role": "user", "content": "hi"}])
    assert seen["body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert seen["body"]["chat_template_kwargs"]["keep"] == 1


def test_one_decide_endpoint_backstops_research_without_xai():
    settings = Settings(
        auth_disabled=True,
        decide_base_url="https://api.together.xyz/v1",
        decide_model="meta-llama/Llama-3-70b",
        decide_api_key="tk",
    )
    registry = build_llm_registry(settings)
    decide = registry.chain_for("decide")
    assert [c.name for c in decide] == ["openai_compat"]
    assert decide[0].model == "meta-llama/Llama-3-70b"
    # Analysts follow the decide endpoint; they are not pinned to Nemotron once it moved.
    assert registry.chain_for("analyst")[0] is decide[0]
    # No xAI credential and no separate research endpoint: the one decide client backstops.
    for role in ("brief", "compiler", "sentiment", "critic"):
        assert registry.chain_for(role) == [decide[0]]


def test_research_endpoint_replaces_xai_and_leaves_decide_on_the_preset():
    settings = Settings(
        auth_disabled=True,
        xai_api_key="k",
        research_base_url="http://localhost:11434/v1",
        research_model="llama3.2",
    )
    registry = build_llm_registry(settings)
    assert [c.name for c in registry.chain_for("decide")] == ["nemotron"]
    assert [c.name for c in registry.chain_for("analyst")] == ["nemotron"]
    research = registry.chain_for("compiler")
    assert [c.name for c in research] == ["openai_compat"]
    assert research[0].model == "llama3.2"
    assert registry.chain_for("brief")[0] is research[0]
    assert registry.chain_for("critic")[0] is research[0]


def test_xai_still_fronts_research_when_only_the_decide_endpoint_moves():
    settings = Settings(
        auth_disabled=True,
        xai_api_key="k",
        decide_base_url="https://api.openai.com/v1",
        decide_model="gpt-4o-mini",
    )
    registry = build_llm_registry(settings)
    assert [c.name for c in registry.chain_for("critic")] == ["xai", "openai_compat"]
    assert [c.name for c in registry.chain_for("analyst")] == ["openai_compat"]


def test_decide_model_required_when_base_url_is_set():
    from trader.llm.registry import build_decide_client

    settings = Settings(auth_disabled=True, decide_base_url="https://api.openai.com/v1")
    with pytest.raises(LLMError, match="TRADER_DECIDE_MODEL"):
        build_decide_client(settings)


async def test_openai_client_sets_bearer_only_when_a_key_is_given():
    authed = OpenAIChatClient(
        name="openai_compat",
        model="m",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
    )
    assert authed._client.headers["Authorization"] == "Bearer sk-test"
    await authed.close()
    bare = OpenAIChatClient(name="openai_compat", model="m", base_url="http://localhost:11434/v1")
    assert "authorization" not in {k.lower() for k in bare._client.headers}
    await bare.close()


def test_build_registry_routes_roles_from_config():
    bare = build_llm_registry(Settings(auth_disabled=True))
    assert [c.name for c in bare.chain_for("decide")] == ["nemotron"]
    # No xAI credential: research roles still run, on the Nemotron backstop alone
    # (the Research Desk must work before any Grok credential exists — see
    # docs/lessons.md#macro-view-advisory-firewall).
    assert [c.name for c in bare.chain_for("brief")] == ["nemotron"]
    assert [c.name for c in bare.chain_for("compiler")] == ["nemotron"]

    with_grok = build_llm_registry(Settings(auth_disabled=True, xai_api_key="k"))
    assert [c.name for c in with_grok.chain_for("compiler")] == ["xai", "nemotron"]


async def test_completions_meter_into_usage_daily(db: Database):
    repo = UsageRepo(db)
    registry = LLMRegistry({"decide": [_StubClient("nemotron")]}, meter=UsageMeter(repo))
    await registry.complete("decide", [{"role": "user", "content": "x"}])
    await registry.complete("decide", [{"role": "user", "content": "y"}])

    rows = await repo.for_day(datetime.now(tz=UTC).date())
    assert len(rows) == 1
    row = rows[0]
    assert (row["role"], row["provider"]) == ("decide", "nemotron")
    assert row["requests"] == 2
    assert row["prompt_tokens"] == 200 and row["completion_tokens"] == 20  # non-zero, real
    assert float(row["cost_usd"]) == pytest.approx(0.10)


# --- grammar-safe wire schema (#70) ------------------------------------------

# Every bound at or above llama.cpp's GBNF repetition cap, at every nesting level the
# pydantic schema generator can produce; plus the values that must survive untouched.
GRAMMAR_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string", "maxLength": 2000},
        "short": {"type": "string", "maxLength": 1999},
        "free": {"type": "string"},
        "tags": {
            "type": "array",
            "maxItems": 4000,
            "items": {"type": "string", "maxLength": 2000},
        },
        "either": {"anyOf": [{"type": "string", "maxLength": 2500}, {"type": "null"}]},
        "leg": {"$ref": "#/$defs/Leg"},
    },
    "$defs": {
        "Leg": {
            "type": "object",
            "properties": {"note": {"type": "string", "maxLength": 2000}},
            "allOf": [{"oneOf": [{"maxLength": 3000}]}],
        }
    },
    "required": ["rationale", "short", "free", "tags", "either", "leg"],
}


def _generic(seen: dict, **overrides) -> OpenAIChatClient:
    from trader.llm.registry import build_decide_client

    settings = Settings(
        auth_disabled=True,
        decide_base_url=overrides.pop("decide_base_url", "http://mars:11434/v1"),
        decide_model=overrides.pop("decide_model", "ornith-15-35b"),
        **overrides,
    )
    return build_decide_client(settings, client=capture_client(seen, VLLM_RESPONSE))


async def test_generic_client_caps_schema_bounds_below_the_grammar_repetition_cap():
    """Ollama/llama.cpp compile the schema to GBNF and 400 on any bound >= 2000 (#70)."""
    seen: dict = {}
    original = json.loads(json.dumps(GRAMMAR_SCHEMA))
    await _generic(seen).complete([{"role": "user", "content": "decide"}], schema=GRAMMAR_SCHEMA)

    wire = seen["body"]["response_format"]["json_schema"]["schema"]
    props = wire["properties"]
    assert props["rationale"]["maxLength"] == 1999
    assert props["short"]["maxLength"] == 1999  # already under the cap: untouched
    assert "maxLength" not in props["free"]  # absent stays absent
    assert props["tags"]["maxItems"] == 1999
    assert props["tags"]["items"]["maxLength"] == 1999
    assert props["either"]["anyOf"][0]["maxLength"] == 1999
    assert props["either"]["anyOf"][1] == {"type": "null"}
    leg = wire["$defs"]["Leg"]
    assert leg["properties"]["note"]["maxLength"] == 1999
    assert leg["allOf"][0]["oneOf"][0]["maxLength"] == 1999
    assert wire["required"] == original["required"]
    assert seen["body"]["response_format"]["json_schema"]["strict"] is True
    assert GRAMMAR_SCHEMA == original  # the caller's schema is not mutated


async def test_generic_client_sends_the_real_decision_schema_grammar_safe():
    from trader.loop.decider import DECISION_SCHEMA

    seen: dict = {}
    await _generic(seen).complete([{"role": "user", "content": "decide"}], schema=DECISION_SCHEMA)
    wire = seen["body"]["response_format"]["json_schema"]["schema"]
    assert wire["properties"]["rationale"]["maxLength"] == 1999
    assert DECISION_SCHEMA["properties"]["rationale"]["maxLength"] == 2000  # untouched


async def test_nemotron_preset_sends_the_schema_untouched():
    """The Spark's decide lane stays byte-identical: vLLM accepts 2000."""
    from trader.loop.decider import DECISION_SCHEMA

    seen: dict = {}
    client = nemotron_client(SETTINGS, client=capture_client(seen, VLLM_RESPONSE))
    await client.complete([{"role": "user", "content": "decide"}], schema=DECISION_SCHEMA)
    assert seen["body"]["response_format"]["json_schema"]["schema"] == DECISION_SCHEMA
    assert seen["body"]["response_format"]["json_schema"]["schema"]["properties"]["rationale"] == {
        "maxLength": 2000,
        "minLength": 1,
        "title": "Rationale",
        "type": "string",
    }


async def test_generic_client_on_the_nemotron_url_sends_the_schema_untouched():
    seen: dict = {}
    client = _generic(
        seen, decide_base_url=SETTINGS.nemotron_base_url, decide_model="nemotron-3-super"
    )
    await client.complete([{"role": "user", "content": "decide"}], schema=GRAMMAR_SCHEMA)
    assert seen["body"]["response_format"]["json_schema"]["schema"] == GRAMMAR_SCHEMA
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_research_client_is_grammar_safe_unless_on_the_preset_url():
    from trader.llm.registry import build_research_client

    seen: dict = {}
    settings = Settings(
        auth_disabled=True, research_base_url="http://mars:11434/v1", research_model="qwen3"
    )
    client = build_research_client(settings, client=capture_client(seen, VLLM_RESPONSE))
    assert client is not None
    await client.complete([{"role": "user", "content": "brief"}], schema=GRAMMAR_SCHEMA)
    assert seen["body"]["response_format"]["json_schema"]["schema"]["properties"]["rationale"] == {
        "type": "string",
        "maxLength": 1999,
    }

    on_preset: dict = {}
    settings = Settings(
        auth_disabled=True, research_base_url=SETTINGS.nemotron_base_url, research_model="n"
    )
    client = build_research_client(settings, client=capture_client(on_preset, VLLM_RESPONSE))
    assert client is not None
    await client.complete([{"role": "user", "content": "brief"}], schema=GRAMMAR_SCHEMA)
    assert on_preset["body"]["response_format"]["json_schema"]["schema"] == GRAMMAR_SCHEMA


def test_decision_output_still_rejects_a_rationale_over_the_real_limit():
    """Only the wire schema is capped; the contract on the reply stays 2000."""
    from trader.loop.decider import DecisionOutput

    DecisionOutput(action="hold", conviction=0.1, rationale="x" * 2000)
    with pytest.raises(ValidationError):
        DecisionOutput(action="hold", conviction=0.1, rationale="x" * 2001)
