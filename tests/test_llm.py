"""LLM layer tests — recorded fixtures, no live model calls (a Spark smoke runs
outside pytest). The usage test hits the real compose Postgres.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from trader.llm.base import Completion, LLMError, Usage
from trader.llm.nemotron import nemotron_client
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
    with pytest.raises(KeyError, match="no price entry"):
        price_per_million("xai", "grok-99")  # unknown models fail loud, never meter $0


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


def test_build_registry_routes_roles_from_config():
    bare = build_llm_registry(Settings(auth_disabled=True))
    assert [c.name for c in bare.chain_for("decide")] == ["nemotron"]
    # No xAI credential: research roles still run, on the Nemotron backstop alone
    # (EAI-178 — the Research Desk must work before any Grok credential exists).
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
