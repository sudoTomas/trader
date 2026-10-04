"""Jev decide provider: TypeSafe's judgment model answers the decide role natively.

Jev takes a state plus typed questions and returns probabilities; it writes no text.
The client turns those answers into a DecisionOutput the decider validates as usual.
"""

from __future__ import annotations

import json

import httpx
import pytest

from trader.llm.base import LLMError
from trader.llm.jev import JEV_PROVIDER, QUESTIONS, JevClient, holds_position, to_decision
from trader.llm.registry import build_decide_client, build_llm_registry
from trader.loop.decider import DECISION_SCHEMA, DecisionOutput
from trader.ops.config import Settings

FLAT = "Symbol: ETH/USD (crypto)\nOpen position: 0 units\n"
HELD = "Symbol: ETH/USD (crypto)\nOpen position: 0.5 units\n"


def answers(action="hold", research="long", edge="1", lean="sell"):
    return {
        "action": {"choice": action, "probabilities": {"hold": 0.98, "open": 0.02, "close": 0.0}},
        "open_side": {"choice": lean, "probabilities": {lean: 0.7}},
        "close_side": {"choice": "sell", "probabilities": {"sell": 0.9}},
        "research": {"choice": research, "probabilities": {research: 0.91}},
        "edge": {"score": 0.9, "probabilities": {edge: 0.7}},
    }


# --- answers -> DecisionOutput ------------------------------------------------------------


def test_hold_maps_to_a_valid_hold():
    d = to_decision(answers(), FLAT, notional_usd=750, research_led_open=False)
    assert (d["action"], d["side"], d["notional_usd"]) == ("hold", None, 0)
    assert d["conviction"] == 0.98
    assert d["rationale"].startswith("Jev judged hold at 98%")
    DecisionOutput.model_validate(d).to_decision("ETH/USD")


def test_research_led_open_is_dark_by_default():
    d = to_decision(answers(), FLAT, notional_usd=750, research_led_open=False)
    assert d["action"] == "hold"


def test_research_long_weak_edge_flat_opens_long_when_enabled():
    d = to_decision(answers(), FLAT, notional_usd=750, research_led_open=True)
    assert (d["action"], d["side"], d["notional_usd"], d["conviction"]) == (
        "open",
        "buy",
        750,
        0.91,
    )
    assert d["rationale"].startswith("Research-led open")
    intent = DecisionOutput.model_validate(d).to_decision("ETH/USD").intent
    assert intent is not None and intent.notional == 750


def test_research_led_open_needs_an_edge():
    assert (
        to_decision(answers(edge="0"), FLAT, notional_usd=750, research_led_open=True)["action"]
        == "hold"
    )


def test_research_led_open_needs_research_long():
    for research in ("silent", "short"):
        d = to_decision(answers(research=research), FLAT, notional_usd=750, research_led_open=True)
        assert d["action"] == "hold"


def test_research_led_open_never_adds_to_a_held_position():
    d = to_decision(answers(), HELD, notional_usd=750, research_led_open=True)
    assert (d["action"], d["side"], d["notional_usd"]) == ("hold", None, 0)


def test_unknown_position_counts_as_held():
    assert not holds_position(FLAT)
    assert holds_position(HELD)
    assert holds_position("no position line")
    d = to_decision(answers(), "no position line", notional_usd=750, research_led_open=True)
    assert d["action"] == "hold"


def test_jev_own_open_keeps_its_side():
    d = to_decision(
        answers(action="open", research="silent", lean="sell"),
        FLAT,
        notional_usd=750,
        research_led_open=True,
    )
    assert (d["action"], d["side"], d["notional_usd"]) == ("open", "sell", 750)


def test_jev_close_uses_close_side():
    d = to_decision(answers(action="close"), HELD, notional_usd=750, research_led_open=False)
    assert (d["action"], d["side"]) == ("close", "sell")


# --- the client on the wire ---------------------------------------------------------------


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


async def test_complete_sends_state_and_questions_and_meters_usage():
    seen: dict = {}
    client = make_client(seen)
    completion = await client.complete(MESSAGES, schema=DECISION_SCHEMA)
    assert seen["url"] == "https://jev.test/v1/systemone"
    assert seen["auth"] == "Bearer jev-test-key"
    assert seen["body"]["model"] == "jev-latest"
    assert seen["body"]["state"] == {"prompt": FLAT, "rules": "rules text"}
    assert seen["body"]["questions"] == QUESTIONS
    assert completion.provider == JEV_PROVIDER
    assert (completion.usage.prompt_tokens, completion.usage.completion_tokens) == (900, 40)
    assert completion.cost_usd == pytest.approx(940 * 0.042 / 1_000_000)
    DecisionOutput.model_validate_json(completion.text)


async def test_complete_refuses_other_schemas():
    client = make_client({})
    with pytest.raises(LLMError, match="only serves the decide schema"):
        await client.complete(MESSAGES, schema={"type": "object", "properties": {"ok": {}}})
    with pytest.raises(LLMError, match="only serves the decide schema"):
        await client.complete(MESSAGES)


async def test_http_error_reports_status_only():
    client = make_client({}, status=401)
    with pytest.raises(LLMError) as exc:
        await client.complete(MESSAGES, schema=DECISION_SCHEMA)
    assert "401" in str(exc.value)
    assert "secret-bearing" not in str(exc.value)
    assert "jev-test-key" not in str(exc.value)


async def test_malformed_answers_raise():
    client = make_client({}, payload={"answers": {}, "usage": {}})
    with pytest.raises(LLMError, match="malformed"):
        await client.complete(MESSAGES, schema=DECISION_SCHEMA)


# --- selection by setting -----------------------------------------------------------------


def test_default_settings_do_not_select_jev():
    settings = Settings(_env_file=None)
    assert settings.decide_provider == ""
    assert build_decide_client(settings).name != JEV_PROVIDER


def test_decide_provider_jev_selects_jev():
    settings = Settings(_env_file=None, decide_provider="jev", jev_api_key="k")
    client = build_decide_client(settings)
    assert isinstance(client, JevClient)
    assert client.model == "jev-latest"


def test_jev_without_key_refuses():
    with pytest.raises(LLMError, match="TRADER_JEV_API_KEY"):
        build_decide_client(Settings(_env_file=None, decide_provider="jev"))


def test_unknown_decide_provider_rejected():
    with pytest.raises(ValueError):
        Settings(_env_file=None, decide_provider="other")


def test_jev_is_not_an_analyst_or_research_backstop():
    registry = build_llm_registry(Settings(_env_file=None, decide_provider="jev", jev_api_key="k"))
    assert isinstance(registry.chain_for("decide")[0], JevClient)
    with pytest.raises(LLMError):
        registry.chain_for("analyst")
    for role in ("brief", "compiler", "sentiment", "critic"):
        assert not any(isinstance(c, JevClient) for c in registry._routes.get(role, []))


# --- doctor ---------------------------------------------------------------------------------


async def test_doctor_probes_jev_with_a_decide_call():
    from trader.llm.registry import LLMRegistry
    from trader.ops.doctor import check_decide

    seen: dict = {}
    settings = Settings(_env_file=None, decide_provider="jev", jev_api_key="k")
    registry = LLMRegistry({"decide": [make_client(seen)]}, None)
    result = await check_decide(settings, registry=registry)
    assert result.status == "ok"
    assert result.data["provider"] == JEV_PROVIDER
    assert seen["body"]["state"]["prompt"].startswith("Symbol: PROBE")
    assert result.env[0] == "TRADER_DECIDE_PROVIDER"
