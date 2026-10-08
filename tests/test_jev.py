"""Jev client: TypeSafe's judgment model answers typed questions with probabilities.

Jev writes no text, so for this client the ``schema`` argument is the question set and the
completion text is Jev's answers as JSON. It is the confirm role and nothing else.
"""

from __future__ import annotations

import json

import httpx
import pytest

from trader.llm.base import LLMError
from trader.llm.jev import JEV_PROVIDER, JevClient, is_question_set
from trader.llm.registry import build_confirm_client, build_decide_client, build_llm_registry
from trader.loop.confirm import QUESTIONS
from trader.loop.decider import DECISION_SCHEMA
from trader.ops.config import Settings

FLAT = "Symbol: ETH/USD (crypto)\nOpen position: 0 units\n"
ASKED = {"action": QUESTIONS["action"], "open_side": QUESTIONS["open_side"]}


def answers():
    return {
        "action": {"choice": "hold", "probabilities": {"hold": 0.61, "open": 0.31, "close": 0.08}},
        "open_side": {"choice": "buy", "probabilities": {"buy": 0.7, "sell": 0.3}},
    }


def jev_transport(seen: dict, payload: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        if status != 200:
            return httpx.Response(status, json={"error": "secret-bearing body"})
        return httpx.Response(
            200,
            json=payload
            or {"answers": answers(), "usage": {"input_tokens": 900, "output_tokens": 40}},
        )

    return httpx.MockTransport(handler)


def make_client(seen: dict, **kwargs) -> JevClient:
    transport = jev_transport(seen, kwargs.pop("payload", None), kwargs.pop("status", 200))
    return JevClient(
        api_key="jev-test-key",
        base_url="https://jev.test/v1/systemone",
        client=httpx.AsyncClient(transport=transport),
        price_in=0.042,
        price_out=0.042,
        **kwargs,
    )


MESSAGES = [
    {"role": "system", "content": "rules text"},
    {"role": "user", "content": FLAT},
]


# --- the client on the wire ---------------------------------------------------------------


async def test_complete_sends_state_and_questions_and_meters_usage():
    seen: dict = {}
    completion = await make_client(seen).complete(MESSAGES, schema=ASKED)
    assert seen["url"] == "https://jev.test/v1/systemone"
    assert seen["auth"] == "Bearer jev-test-key"
    assert seen["body"]["model"] == "jev-latest"
    assert seen["body"]["state"] == {"prompt": FLAT, "rules": "rules text"}
    assert seen["body"]["questions"] == ASKED
    assert completion.provider == JEV_PROVIDER
    assert (completion.usage.prompt_tokens, completion.usage.completion_tokens) == (900, 40)
    assert completion.cost_usd == pytest.approx(940 * 0.042 / 1_000_000)
    assert json.loads(completion.text) == answers()


async def test_complete_refuses_json_schemas():
    """Jev can never answer as a decider, analyst or research role."""
    client = make_client({})
    assert not is_question_set(DECISION_SCHEMA)
    for schema in (DECISION_SCHEMA, {"type": "object", "properties": {"ok": {}}}, None, {}):
        with pytest.raises(LLMError, match="only answers a question set"):
            await client.complete(MESSAGES, schema=schema)


async def test_http_error_reports_status_only():
    client = make_client({}, status=401)
    with pytest.raises(LLMError) as exc:
        await client.complete(MESSAGES, schema=ASKED)
    assert "401" in str(exc.value)
    assert "secret-bearing" not in str(exc.value)
    assert "jev-test-key" not in str(exc.value)


async def test_missing_answers_raise_malformed():
    client = make_client({}, payload={"answers": {"action": {}}, "usage": {}})
    with pytest.raises(LLMError, match="malformed"):
        await client.complete(MESSAGES, schema=ASKED)


# --- selection by setting -----------------------------------------------------------------


def test_default_settings_configure_no_confirm():
    settings = Settings(_env_file=None)
    assert settings.confirm_provider == ""
    assert build_confirm_client(settings) is None
    assert build_decide_client(settings).name != JEV_PROVIDER
    with pytest.raises(LLMError):
        build_llm_registry(settings).chain_for("confirm")


def test_confirm_provider_jev_selects_jev():
    settings = Settings(
        _env_file=None,
        confirm_provider="jev",
        confirm_api_key="k",
        confirm_base_url="http://127.0.0.1:8011/v1/systemone",
        confirm_model="kev-latest",
    )
    client = build_confirm_client(settings)
    assert isinstance(client, JevClient)
    assert (client.model, client._url) == ("kev-latest", "http://127.0.0.1:8011/v1/systemone")


def test_jev_without_key_refuses():
    with pytest.raises(LLMError, match="TRADER_CONFIRM_API_KEY"):
        build_confirm_client(Settings(_env_file=None, confirm_provider="jev"))


def test_unknown_confirm_provider_rejected():
    with pytest.raises(ValueError):
        Settings(_env_file=None, confirm_provider="other")


def test_retired_jev_decide_settings_are_ignored():
    """A leftover TRADER_DECIDE_PROVIDER=jev no longer moves the decide seat."""
    settings = Settings(_env_file=None, decide_provider="jev", jev_api_key="k")
    assert not hasattr(settings, "decide_provider")
    assert build_decide_client(settings).name != JEV_PROVIDER


def test_jev_is_only_the_confirm_role():
    registry = build_llm_registry(
        Settings(_env_file=None, confirm_provider="jev", confirm_api_key="k")
    )
    assert [type(c) for c in registry.chain_for("confirm")] == [JevClient]
    # The decide seat is the Nemotron preset again, and the analyst backstop is back.
    assert [c.name for c in registry.chain_for("decide")] == ["nemotron"]
    assert [c.name for c in registry.chain_for("analyst")] == ["nemotron"]
    for role in ("decide", "analyst", "brief", "compiler", "sentiment", "critic"):
        assert not any(isinstance(c, JevClient) for c in registry._routes.get(role, []))
