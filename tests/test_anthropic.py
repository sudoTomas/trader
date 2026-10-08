"""Native Anthropic adapter (#104): Claude serves any chat Role through the official SDK.
Seams: the client as an ``LLMClient``, ProviderDesk's model list for an Anthropic
Provider, and ProfileDesk (a Claude Chain saves after its Probe and serves through the
shared registry). The SDK is built on httpx2, so Anthropic HTTP is mocked with an
``httpx2.MockTransport``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from cryptography.fernet import Fernet

from trader.desk.critic import CRITIC_SCHEMA, CriticOutput
from trader.llm.anthropic import AnthropicClient
from trader.llm.base import LLMError
from trader.loop.decider import DECISION_SCHEMA, SYSTEM_PROMPT, DecisionOutput
from trader.store.db import Database

API_KEY = "sk-ant-api03-very-secret-wxyz"
DECIDE = {
    "action": "open",
    "conviction": 0.7,
    "rationale": "RSI 31 under SMA",
    "side": "buy",
    "notional_usd": 400,
}


class Claude:
    """The Anthropic API at the transport: scripted replies, recorded requests."""

    def __init__(self, text: str = "", *, status: int = 200, models: list[str] | None = None):
        self.text, self.status, self.models = text, status, models or []
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.url.path, dict(request.headers), body))
        if self.status != 200:
            return httpx2.Response(
                self.status,
                json={"type": "error", "error": {"type": "api_error", "message": API_KEY}},
            )
        if request.url.path.endswith("/models"):
            data = [
                {"id": m, "type": "model", "display_name": m, "created_at": "2026-01-01T00:00:00Z"}
                for m in self.models
            ]
            return httpx2.Response(200, json={"data": data, "has_more": False})
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": self.text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 812, "output_tokens": 64},
            },
        )


def client(server: Claude, model: str = "claude-opus-5-5") -> AnthropicClient:
    return AnthropicClient(
        name="claude",
        model=model,
        api_key=API_KEY,
        base_url="https://api.anthropic.com/v1",
        transport=httpx2.MockTransport(server),
    )


def keys_of(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        return set(schema) | {k for v in schema.values() for k in keys_of(v)}
    if isinstance(schema, list):
        return {k for v in schema for k in keys_of(v)}
    return set()


MESSAGES = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": "Symbol: SPY. Price 500."},
]


# --- the client -----------------------------------------------------------------------------


async def test_a_decide_completion_validates_as_a_decision():
    server = Claude(json.dumps(DECIDE))

    completion = await client(server).complete(MESSAGES, schema=DECISION_SCHEMA, max_tokens=512)

    decision = DecisionOutput.model_validate_json(completion.text).to_decision("SPY")
    assert (decision.action.value, decision.intent.notional) == ("open", 400)
    path, headers, body = server.requests[0]
    assert path == "/v1/messages"
    assert headers["x-api-key"] == API_KEY
    # System goes to `system`, the rest to `messages`; the schema to output_config.format.
    assert body["system"] == SYSTEM_PROMPT
    assert body["messages"] == [{"role": "user", "content": "Symbol: SPY. Price 500."}]
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(DECIDE)


async def test_the_critic_schema_is_honored():
    reply = {"verdict": "shrink", "notional_usd": 200, "reason": "thin evidence"}
    server = Claude(json.dumps(reply))

    completion = await client(server).complete(MESSAGES, schema=CRITIC_SCHEMA)

    assert CriticOutput.model_validate_json(completion.text).verdict == "shrink"
    sent = server.requests[0][2]["output_config"]["format"]["schema"]
    assert sent["properties"]["verdict"]["enum"] == ["approve", "shrink", "veto"]
    # Bounds the API does not take leave the wire schema (as description hints);
    # pydantic still enforces them when the caller validates.
    assert not {"minLength", "maxLength", "minimum", "maximum"} & keys_of(sent)


async def test_usage_is_the_providers_own_count():
    completion = await client(Claude("{}")).complete(MESSAGES)
    assert (completion.usage.prompt_tokens, completion.usage.completion_tokens) == (812, 64)
    assert (completion.provider, completion.model) == ("claude", "claude-opus-5-5")


async def test_no_schema_sends_no_output_format():
    server = Claude("plain text")
    completion = await client(server).complete(MESSAGES)
    assert completion.text == "plain text"
    assert "output_config" not in server.requests[0][2]


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_an_http_error_reports_status_only_and_never_the_key(status: int):
    with pytest.raises(LLMError) as raised:
        await client(Claude(status=status)).complete(MESSAGES, schema=DECISION_SCHEMA)
    assert str(raised.value) == f"claude: HTTP {status}"
    assert API_KEY not in str(raised.value) and API_KEY not in repr(raised.value.__cause__)


async def test_an_unreachable_endpoint_is_an_llm_error():
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    unreachable = AnthropicClient(
        name="claude",
        model="claude-opus-5-5",
        api_key=API_KEY,
        base_url="https://api.anthropic.com/v1",
        transport=httpx2.MockTransport(refuse),
    )
    with pytest.raises(LLMError, match=r"^claude: unreachable \(APIConnectionError\)$"):
        await unreachable.complete(MESSAGES)


# --- ProviderDesk: the Models API -----------------------------------------------------------

SECRETS_KEY = Fernet.generate_key().decode()


async def test_an_anthropic_provider_lists_its_models(db: Database):
    from trader.llm.providers import ProviderDesk

    server = Claude(models=["claude-opus-5-5", "claude-haiku-5-5"])
    providers = ProviderDesk(
        db, secrets_key=SECRETS_KEY, anthropic_transport=httpx2.MockTransport(server)
    )
    await providers.add(name="claude", preset="anthropic", api_key=API_KEY)

    assert await providers.models("claude") == ["claude-haiku-5-5", "claude-opus-5-5"]
    path, headers, _ = server.requests[0]
    assert (path, headers["x-api-key"]) == ("/v1/models", API_KEY)
    assert providers.presets()["anthropic"]["default_model"] == "claude-opus-5-5"


# --- ProfileDesk: a Claude Chain ------------------------------------------------------------


async def test_a_claude_chain_saves_after_its_probe_and_serves_the_role(db: Database):
    from trader.llm.profiles import ProfileDesk
    from trader.llm.providers import ProviderDesk
    from trader.llm.registry import LLMRegistry

    hold = {**DECIDE, "action": "hold", "side": None, "notional_usd": None}
    server = Claude(json.dumps(hold))
    transport = httpx2.MockTransport(server)
    providers = ProviderDesk(db, secrets_key=SECRETS_KEY, anthropic_transport=transport)
    profiles = ProfileDesk(db, providers, anthropic_transport=transport)
    await providers.add(name="claude", preset="anthropic", api_key=API_KEY)
    await profiles.create("trial")

    # No model named: the Anthropic preset's default fills it.
    saved = await profiles.set_chain("trial", "decide", [{"provider": "claude", "model": ""}])
    completion = await LLMRegistry({}, None, profiles=profiles).complete("decide", MESSAGES)

    assert saved["profile"]["roles"]["decide"] == [
        {"provider": "claude", "model": "claude-opus-5-5"}
    ]
    assert saved["probe"][0]["ok"] is True
    assert (completion.provider, completion.model) == ("claude", "claude-opus-5-5")
    assert completion.profile_version is not None
    assert len(server.requests) == 2  # the Probe, then the served call
