"""xAI endpoint/tool probe — the in-service diagnostic behind `trader xai probe` (#7).

All HTTP is ``httpx.MockTransport``; nothing here ever reaches api.x.ai. The live matrix is
recorded by the operator in docs/research/xai-api.md.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client
from typer.testing import CliRunner

from trader.llm import xai_probe
from trader.llm.xai import ManagedBearerAuth, xai_http_client
from trader.llm.xai_probe import ProbeResult, probe, probe_matrix
from trader.ops.config import Settings
from trader.store.db import Database
from trader.surfaces.api.app import create_app
from trader.surfaces.mcp.server import build_mcp
from trader.surfaces.services import build_registry

BASE = "https://api.x.ai/v1"


def chat_ok(model: str = "grok-4") -> dict:
    return {
        "id": "c1",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 1},
    }


def responses_ok(citations: int = 0) -> dict:
    annotations = [
        {"type": "url_citation", "url": f"https://x.com/u/status/{i}"} for i in range(citations)
    ]
    return {
        "id": "r1",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "ok", "annotations": annotations}],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 2},
    }


def client_with(handler, auth: httpx.Auth | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler), auth=auth)


class FakeManager:
    """Stands in for XaiCredentialManager: serves one token, counts calls."""

    def __init__(self, token: str = "oauth-token") -> None:
        self.token = token
        self.calls = 0

    async def get_access_token(self) -> str:
        self.calls += 1
        return self.token


# --- request shapes -----------------------------------------------------------


async def test_chat_completions_plain_shape():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=chat_ok())

    async with client_with(handler) as client:
        result = await probe(client, endpoint="chat/completions", model="grok-4")

    assert isinstance(result, ProbeResult)
    assert result.ok is True and result.status_code == 200 and result.error_class is None
    assert result.endpoint == "chat/completions" and result.model == "grok-4"
    assert result.tool is None
    assert result.usage == {"input_tokens": 7, "output_tokens": 1}
    assert result.citations_count == 0
    assert result.latency_ms >= 0

    (request,) = seen
    assert request.method == "POST"
    assert request.url == f"{BASE}/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "grok-4"
    assert body["messages"][0]["role"] == "user"
    assert body["max_tokens"] <= 16
    assert "tools" not in body


async def test_responses_plain_shape():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=responses_ok())

    async with client_with(handler) as client:
        result = await probe(client, endpoint="responses", model="grok-4.3")

    assert result.ok is True and result.usage == {"input_tokens": 11, "output_tokens": 2}
    (request,) = seen
    assert request.url == f"{BASE}/responses"
    body = json.loads(request.content)
    assert body["model"] == "grok-4.3"
    assert body["input"][0]["role"] == "user"
    assert body["max_output_tokens"] <= 16
    assert body["store"] is False
    assert "tools" not in body


@pytest.mark.parametrize("endpoint", ["chat/completions", "responses"])
async def test_x_search_tool_has_one_day_window(endpoint: str):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = chat_ok() if endpoint == "chat/completions" else responses_ok(citations=3)
        return httpx.Response(200, json=payload)

    async with client_with(handler) as client:
        result = await probe(client, endpoint=endpoint, model="grok-4.3", tool="x_search")

    assert result.tool == "x_search" and result.ok is True
    (request,) = seen
    body = json.loads(request.content)
    (tool,) = body["tools"]
    assert tool["type"] == "x_search"
    today = datetime.now(tz=UTC).date()
    assert tool["to_date"] == today.isoformat()
    assert tool["from_date"] == (today - timedelta(days=1)).isoformat()
    prompt = body["messages"][0]["content"] if "messages" in body else body["input"][0]["content"]
    assert "test" in prompt.lower()
    if endpoint == "responses":
        assert result.citations_count == 3


@pytest.mark.parametrize("endpoint", ["chat/completions", "responses"])
async def test_web_search_tool_shape(endpoint: str):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = chat_ok() if endpoint == "chat/completions" else responses_ok()
        return httpx.Response(200, json=payload)

    async with client_with(handler) as client:
        result = await probe(client, endpoint=endpoint, model="grok-4-1-fast", tool="web_search")

    assert result.ok is True and result.tool == "web_search"
    (request,) = seen
    (tool,) = json.loads(request.content)["tools"]
    assert tool == {"type": "web_search"}


async def test_chat_top_level_citations_are_counted():
    payload = {**chat_ok(), "citations": ["https://a.example", "https://b.example"]}
    async with client_with(lambda r: httpx.Response(200, json=payload)) as client:
        result = await probe(client, endpoint="chat/completions", model="grok-4", tool="web_search")
    assert result.citations_count == 2


async def test_unknown_endpoint_or_tool_is_a_caller_error():
    async with client_with(lambda r: httpx.Response(200, json=chat_ok())) as client:
        with pytest.raises(ValueError, match="endpoint"):
            await probe(client, endpoint="messages", model="grok-4")
        with pytest.raises(ValueError, match="tool"):
            await probe(client, endpoint="responses", model="grok-4", tool="code_execution")
        with pytest.raises(ValueError, match="credential"):
            await probe(client, endpoint="responses", model="grok-4", credential="magic")


# --- classification -------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_401_and_403_classify_as_entitlement(status: int):
    handler = lambda r: httpx.Response(status, json={"error": "no access"})  # noqa: E731
    async with client_with(handler) as client:
        result = await probe(client, endpoint="responses", model="grok-4.3", tool="x_search")
    assert result.ok is False
    assert result.status_code == status
    assert result.error_class == "entitlement"
    assert result.usage == {"input_tokens": 0, "output_tokens": 0}
    assert "no access" in result.detail


@pytest.mark.parametrize(
    ("status", "error_class"),
    [
        (400, "bad_request"),
        (404, "bad_request"),
        (422, "bad_request"),
        (429, "rate_limited"),
        (500, "server_error"),
        (503, "server_error"),
    ],
)
async def test_other_http_statuses_classify(status: int, error_class: str):
    async with client_with(lambda r: httpx.Response(status, text="nope")) as client:
        result = await probe(client, endpoint="chat/completions", model="grok-4")
    assert result.ok is False and result.status_code == status
    assert result.error_class == error_class


async def test_timeout_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with client_with(handler) as client:
        result = await probe(client, endpoint="responses", model="grok-4")
    assert result.ok is False
    assert result.status_code is None
    assert result.error_class == "timeout"


async def test_transport_error_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with client_with(handler) as client:
        result = await probe(client, endpoint="responses", model="grok-4")
    assert result.ok is False and result.error_class == "transport"


async def test_2xx_without_json_is_malformed():
    async with client_with(lambda r: httpx.Response(200, text="<html>")) as client:
        result = await probe(client, endpoint="chat/completions", model="grok-4")
    assert result.ok is False and result.status_code == 200
    assert result.error_class == "malformed"


# --- credential reporting -------------------------------------------------------


async def test_reports_oauth_when_the_manager_serves_a_token():
    manager = FakeManager()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=responses_ok())

    auth = ManagedBearerAuth(manager, "fallback-key")  # type: ignore[arg-type]
    async with client_with(handler, auth=auth) as client:
        result = await probe(client, endpoint="responses", model="grok-4")
    assert result.credential_used == "oauth"
    assert seen == ["Bearer oauth-token"] and manager.calls == 1


async def test_reports_api_key_when_no_manager():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=chat_ok())

    async with client_with(handler, auth=ManagedBearerAuth(None, "plain-key")) as client:
        result = await probe(client, endpoint="chat/completions", model="grok-4")
    assert result.credential_used == "api_key"
    assert seen == ["Bearer plain-key"]


async def test_credential_can_be_forced_either_way():
    manager = FakeManager()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=responses_ok())

    auth = ManagedBearerAuth(manager, "plain-key")  # type: ignore[arg-type]
    async with client_with(handler, auth=auth) as client:
        forced_key = await probe(client, endpoint="responses", model="grok-4", credential="api_key")
        forced_oauth = await probe(client, endpoint="responses", model="grok-4", credential="oauth")
    assert forced_key.credential_used == "api_key"
    assert forced_oauth.credential_used == "oauth"
    assert seen == ["Bearer plain-key", "Bearer oauth-token"]
    assert manager.calls == 1  # the forced api_key probe never touched the lineage


async def test_forced_credential_that_is_absent_is_no_credential_not_a_raise():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=responses_ok())

    async with client_with(handler, auth=ManagedBearerAuth(None, "plain-key")) as client:
        result = await probe(client, endpoint="responses", model="grok-4", credential="oauth")
    assert result.ok is False and result.error_class == "no_credential"
    assert result.credential_used is None and result.status_code is None
    assert calls == 0  # nothing left the process

    async with client_with(handler, auth=ManagedBearerAuth(None, "")) as client:
        result = await probe(client, endpoint="responses", model="grok-4")
    assert result.error_class == "no_credential" and calls == 0


async def test_default_auth_flow_is_unchanged_without_a_forced_credential():
    """The extension is opt-in: the roles' requests keep resolving OAuth-then-key."""
    manager = FakeManager()
    auth = ManagedBearerAuth(manager, "plain-key")  # type: ignore[arg-type]
    async with client_with(lambda r: httpx.Response(200, json={}), auth=auth) as client:
        resp = await client.post("/chat/completions", json={})
    assert resp.request.headers["Authorization"] == "Bearer oauth-token"


def test_xai_http_client_is_the_roles_credential_path():
    assert xai_http_client(Settings(auth_disabled=True)) is None  # no credential path at all
    client = xai_http_client(Settings(auth_disabled=True, xai_api_key="k"))
    assert isinstance(client, httpx.AsyncClient)
    assert isinstance(client.auth, ManagedBearerAuth)
    assert str(client.base_url).rstrip("/") == BASE


# --- matrix ---------------------------------------------------------------------


async def test_matrix_runs_sequentially_with_a_delay(monkeypatch: pytest.MonkeyPatch):
    order: list[tuple[str, str, str | None]] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tool = body["tools"][0]["type"] if "tools" in body else None
        order.append((request.url.path.rsplit("/v1/", 1)[1], body["model"], tool))
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json=responses_ok())
        return httpx.Response(403, text="oauth is /responses-only")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(xai_probe.asyncio, "sleep", fake_sleep)
    async with client_with(handler) as client:
        results = await probe_matrix(
            client,
            models=["grok-4", "grok-4.3"],
            endpoints=["chat/completions", "responses"],
            tools=[None, "x_search"],
            delay_seconds=0.25,
        )

    assert len(results) == 8 and len(order) == 8
    assert order[0] == ("chat/completions", "grok-4", None)
    assert order[-1] == ("responses", "grok-4.3", "x_search")
    assert [r.ok for r in results if r.endpoint == "responses"] == [True] * 4
    assert {r.error_class for r in results if r.endpoint == "chat/completions"} == {"entitlement"}
    assert sleeps == [0.25] * 7  # between calls, never after the last


# --- the operator door: registry, API, MCP, CLI ------------------------------------


def canned() -> list[ProbeResult]:
    return [
        ProbeResult(
            endpoint="responses",
            model="grok-4.3",
            tool="x_search",
            status_code=200,
            ok=True,
            credential_used="oauth",
            error_class=None,
            usage={"input_tokens": 11, "output_tokens": 2},
            latency_ms=1234,
            citations_count=3,
        ),
        ProbeResult(
            endpoint="chat/completions",
            model="grok-4.3",
            tool=None,
            status_code=403,
            ok=False,
            credential_used="oauth",
            error_class="entitlement",
            usage={"input_tokens": 0, "output_tokens": 0},
            latency_ms=90,
            citations_count=0,
            detail="forbidden",
        ),
    ]


def test_probe_xai_is_registered_as_an_operator_command():
    cap = build_registry(None).get("probe_xai")
    assert cap.kind == "command" and cap.tool_name == "probe_xai"


async def test_probe_xai_without_a_credential_is_unavailable(db: Database):
    out = await build_registry(db).get("probe_xai").handler()
    assert out["status"] == "unavailable"
    assert await db.fetch_one("SELECT count(*) AS n FROM audit_events") == {"n": 0}


async def test_probe_xai_rejects_bad_parameters(db: Database):
    registry = build_registry(db, xai_http=object())
    with pytest.raises(ValueError, match="endpoint"):
        await registry.get("probe_xai").handler(endpoints=["messages"])
    with pytest.raises(ValueError, match="tool"):
        await registry.get("probe_xai").handler(tools=["code"])
    with pytest.raises(ValueError, match="model"):
        await registry.get("probe_xai").handler(models=[])


async def test_probe_xai_runs_the_matrix_and_audits_once(
    db: Database, monkeypatch: pytest.MonkeyPatch
):
    calls: list[dict] = []

    async def fake_matrix(client, *, models, endpoints, tools, credential="auto", **kw):
        calls.append(
            {
                "client": client,
                "models": models,
                "endpoints": endpoints,
                "tools": tools,
                "credential": credential,
            }
        )
        return canned()

    monkeypatch.setattr(xai_probe, "probe_matrix", fake_matrix)
    sentinel = object()
    out = await build_registry(db, xai_http=sentinel).get("probe_xai").handler()

    assert calls[0]["client"] is sentinel
    assert calls[0]["models"] == ["grok-4", "grok-4.3", "grok-4-1-fast"]
    assert calls[0]["endpoints"] == ["chat/completions", "responses"]
    assert calls[0]["tools"] == [None, "x_search", "web_search"]
    assert calls[0]["credential"] == "auto"
    assert out["status"] == "ok"
    assert out["summary"] == {"probes": 2, "ok": 1, "entitlement": 1}
    assert out["results"][0]["citations_count"] == 3
    assert out["results"][1]["error_class"] == "entitlement"

    rows = await db.fetch_all("SELECT action, payload FROM audit_events")
    assert [r["action"] for r in rows] == ["probe_xai"]
    payload = rows[0]["payload"]
    assert payload["summary"] == {"probes": 2, "ok": 1, "entitlement": 1}
    assert payload["matrix"][0]["endpoint"] == "responses"
    assert "usage" not in payload["matrix"][0]  # the audit row is a summary, not the dump

    again = (
        await build_registry(db, xai_http=sentinel)
        .get("probe_xai")
        .handler(
            models=["grok-4"],
            endpoints=["responses"],
            tools=["none", "x_search"],
            credential="api_key",
        )
    )
    assert calls[1]["tools"] == [None, "x_search"] and calls[1]["credential"] == "api_key"
    assert again["status"] == "ok"


def test_probe_xai_round_trip_api_mcp_cli(clean_db: None, monkeypatch: pytest.MonkeyPatch):
    async def fake_matrix(client, **kw):
        return canned()

    monkeypatch.setattr(xai_probe, "probe_matrix", fake_matrix)
    import trader.surfaces.api.app as app_mod

    monkeypatch.setattr(app_mod, "build_xai_http", lambda settings: object())
    settings = Settings(auth_disabled=True)

    with TestClient(create_app(settings)) as tc:
        resp = tc.post(
            "/xai/probe",
            json={"models": ["grok-4.3"], "endpoints": ["responses"], "tools": ["x_search"]},
        )
        assert resp.status_code == 200, resp.text
        via_api = resp.json()
        assert tc.post("/xai/probe", json={"endpoints": ["messages"]}).status_code == 422
        audit_rows = tc.get("/audit").json()
    assert via_api["status"] == "ok" and via_api["summary"]["probes"] == 2
    assert [r["action"] for r in audit_rows] == ["probe_xai"]

    async def via_mcp() -> dict:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            registry = build_registry(db, xai_http=object())
            async with Client(build_mcp(registry)) as mcp_client:
                tools = {t.name for t in (await mcp_client.list_tools()).tools}
                assert "probe_xai" in tools
                call = await mcp_client.call_tool("probe_xai", {"models": ["grok-4.3"]})
            return call.structured_content  # a dict result is the structured content itself
        finally:
            await db.close()

    assert asyncio.run(via_mcp())["summary"] == via_api["summary"]

    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(
        cli.app, ["xai", "probe", "--model", "grok-4.3", "--endpoint", "responses", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["summary"] == via_api["summary"]

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    table = CliRunner().invoke(cli.app, ["xai", "probe", "--tool", "x_search", "--tool", "none"])
    assert table.exit_code == 0, table.output
    assert "entitlement" in table.output and "responses" in table.output
    assert "oauth" in table.output
