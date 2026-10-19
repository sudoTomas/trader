"""FRED reaches the decide context fetch-on-use: latest print only, nothing stored.

The client is exercised with a mocked transport. Context and replay tests stub
``get_series`` so the suite never calls api.stlouisfed.org.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx

from tests.test_loop import NOW, StubMarketData
from tests.test_replay_eval import HOLD, seed_hourly_bars
from trader.core.symbols import default_registry
from trader.data.base import MarketDataError
from trader.data.composite import CompositeMarketData, build_market_data
from trader.data.fred import Fred
from trader.eval.replay import ReplayHarness
from trader.loop.context import ContextBuilder
from trader.loop.sources import build_sources
from trader.ops.config import Settings
from trader.store.db import Database
from trader.surfaces.services import build_registry

BTC = default_registry().get("BTC/USD")


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")


class RecordingFred:
    """Returns a fixed ascending series and records the window it was asked for."""

    def __init__(self, series: list[tuple[date, float]]) -> None:
        self._series = series
        self.calls: list[dict] = []

    async def get_series(
        self, series_id: str, limit: int = 30, *, observation_end: date | None = None
    ):
        self.calls.append(
            {"series_id": series_id, "limit": limit, "observation_end": observation_end}
        )
        return list(self._series)


class BoomFred:
    async def get_series(
        self, series_id: str, limit: int = 30, *, observation_end: date | None = None
    ):
        raise MarketDataError("fred down")


def _prints() -> list[tuple[date, float]]:
    day = NOW.date()
    return [
        (day - timedelta(days=2), 16.0),
        (day, 18.2),
        (day + timedelta(days=1), 424242.0),  # after the context clock; must not leak
    ]


# --- client -----------------------------------------------------------------


async def test_fred_observation_end_is_part_of_the_request_and_the_cache_key():
    """A replay step must be able to ask for the window ending that day, not 'latest'."""
    seen: list[dict[str, str]] = []
    end = datetime.now(tz=UTC).date()
    other = end - timedelta(days=1)

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        stamp = params.get("observation_end", end.isoformat())
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": stamp, "value": "18.2"},
                    {"date": "2020-01-01", "value": "."},
                ]
            },
        )

    fred = Fred("key", client=mock_client(handler))
    series = await fred.get_series("VIXCLS", limit=5, observation_end=end)
    assert series == [(end, 18.2)]
    assert seen[0]["series_id"] == "VIXCLS"
    assert seen[0]["observation_end"] == end.isoformat()
    assert "api_key" in seen[0]

    assert await fred.get_series("VIXCLS", limit=5, observation_end=end) == series
    assert len(seen) == 1  # same window is the TTL cache, not a second fetch

    await fred.get_series("VIXCLS", limit=5, observation_end=other)
    assert len(seen) == 2
    assert seen[1]["observation_end"] == other.isoformat()


# --- context ----------------------------------------------------------------


def fred_builder(data, fred=None) -> ContextBuilder:
    """A builder subscribed to the FRED Source only, over the live Source wiring."""
    return ContextBuilder(
        data,
        sources=build_sources(None, data, fred=fred),
        subscriptions=[{"source": "fred", "params": {"series": ["VIXCLS"]}}],
    )


async def test_context_attaches_the_latest_fred_print_on_or_before_now():
    fred = RecordingFred(_prints())
    ctx = await fred_builder(StubMarketData(), fred).build(BTC, now=NOW, triggers=[])

    assert [(p.series_id, p.obs_date, p.value) for p in ctx.fred_series] == [
        ("VIXCLS", NOW.date(), 18.2)
    ]
    assert fred.calls[0]["series_id"] == "VIXCLS"
    assert fred.calls[0]["observation_end"] == NOW.date()
    prompt = ctx.to_prompt()
    assert "VIXCLS" in prompt and "18.2" in prompt
    assert "424242" not in prompt  # the print after `now` is not part of the lane
    assert "sizing" in prompt  # advisory facts, not a risk input


async def test_context_reads_fred_from_the_market_data_provider():
    """The live runner wires Sources over its market data and does not grow a fred kwarg.
    Composite exposes the client; the FRED Source has to pick it up from there."""
    fred = RecordingFred(_prints())

    class _WithFred(StubMarketData):
        pass

    data = _WithFred()
    data.fred = fred
    ctx = await fred_builder(data).build(BTC, now=NOW, triggers=[])
    assert [p.value for p in ctx.fred_series] == [18.2]
    assert fred.calls  # fetched, not a stored window


async def test_context_degrades_when_fred_fails_and_omits_the_lane_when_absent():
    failed = await fred_builder(StubMarketData(), BoomFred()).build(BTC, now=NOW, triggers=[])
    assert failed.price > 0
    assert failed.fred_series == ()
    assert "VIXCLS" not in failed.to_prompt()

    plain = await fred_builder(StubMarketData()).build(BTC, now=NOW, triggers=[])
    assert plain.fred_series == ()
    assert "FRED" not in plain.to_prompt()


# --- composite --------------------------------------------------------------


def test_composite_holds_fred_beside_the_bar_chains():
    fred = RecordingFred([])
    md = CompositeMarketData({BTC.asset_class: [StubMarketData()]}, fred=fred)
    assert md.fred is fred
    assert CompositeMarketData({BTC.asset_class: [StubMarketData()]}).fred is None


def test_build_market_data_wires_fred_only_when_a_key_is_configured():
    keyed = build_market_data(
        Settings(
            auth_disabled=True,
            alpaca_api_key_id="k",
            alpaca_api_secret="s",
            fred_api_key="fred-key",
        )
    )
    assert isinstance(keyed.fred, Fred)

    plain = build_market_data(
        Settings(auth_disabled=True, alpaca_api_key_id="k", alpaca_api_secret="s")
    )
    assert plain.fred is None


# --- replay -----------------------------------------------------------------


class _ReplayProvider(StubMarketData):
    def __init__(self, fred: RecordingFred) -> None:
        super().__init__()
        self.fred = fred


async def test_replay_uses_the_fred_print_current_on_the_step_date(db: Database):
    await seed_hourly_bars(db)
    fred = RecordingFred(_prints())
    harness = ReplayHarness(db, _HoldLLM(), _ReplayProvider(fred))
    start = NOW - timedelta(hours=1)
    run = await harness.run(BTC, start, NOW, label="fred")
    assert run["status"] == "done"
    step = await db.fetch_one("SELECT prompt FROM eval_steps")
    assert step is not None
    assert "18.2" in step["prompt"]
    assert "424242" not in step["prompt"]
    assert fred.calls
    assert all(call["observation_end"] == NOW.date() for call in fred.calls)


class _HoldLLM:
    async def complete(self, role, messages, *, schema=None, max_tokens=1024, temperature=0.2):
        from trader.llm.base import Completion, Usage

        return Completion(
            text=HOLD,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            cost_usd=0.0,
            provider="stub",
            model="stub",
        )


# --- surfaces ---------------------------------------------------------------


def _observations_handler(seen: list[dict[str, str]] | None = None):
    """A FRED-shaped reply: desc order, one missing print, no network."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2026-08-12", "value": "18.2"},
                    {"date": "2026-08-11", "value": "."},
                    {"date": "2026-08-10", "value": "16.0"},
                ]
            },
        )

    return handler


def _runner_with(fred) -> SimpleNamespace:
    return SimpleNamespace(market_data=SimpleNamespace(fred=fred))


async def test_fred_query_returns_the_latest_prints_newest_first(db: Database):
    """The door reads the same client the decide context uses; nothing is stored."""
    seen: list[dict[str, str]] = []
    fred = Fred("key", client=mock_client(_observations_handler(seen)))
    reg = build_registry(db, runner=_runner_with(fred))

    out = await reg.get("fred").handler(series="VIXCLS", limit=3)
    assert out == {
        "series": "VIXCLS",
        "observations": [
            {"date": "2026-08-12", "value": 18.2},
            {"date": "2026-08-10", "value": 16.0},
        ],
    }
    assert seen[0]["series_id"] == "VIXCLS"
    assert seen[0]["limit"] == "3"
    assert reg.get("fred").kind == "query"
    assert reg.get("fred").tool_name == "get_fred"


async def test_fred_query_is_unavailable_without_a_key_or_when_the_provider_fails(
    db: Database, monkeypatch
):
    from trader.ops import config

    monkeypatch.setattr(config, "_settings", Settings(auth_disabled=True))
    out = await build_registry(db).get("fred").handler()
    assert out["status"] == "unavailable"
    assert "FRED_API_KEY" in out["reason"]

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="fred is down")

    fred = Fred("key", client=mock_client(boom))
    out = await build_registry(db, runner=_runner_with(fred)).get("fred").handler(series="DGS10")
    assert out["status"] == "unavailable"
    assert "503" in out["reason"]


def test_fred_surfaced_on_api_mcp_and_cli(clean_db: None, monkeypatch):
    """One capability, three doors: the same observations through REST, MCP, and CLI.
    Without a loop the door builds its own client from FRED_API_KEY, once."""
    import asyncio
    import json

    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.data import fred as fred_mod
    from trader.ops import config
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp

    settings = Settings(auth_disabled=True, fred_api_key="fred-key")
    monkeypatch.setattr(config, "_settings", settings)
    real_fred = fred_mod.Fred
    built: list[str] = []

    def fake_fred(api_key: str, **kwargs):
        built.append(api_key)
        return real_fred(api_key, client=mock_client(_observations_handler()))

    monkeypatch.setattr(fred_mod, "Fred", fake_fred)
    expected = {
        "series": "VIXCLS",
        "observations": [
            {"date": "2026-08-12", "value": 18.2},
            {"date": "2026-08-10", "value": 16.0},
        ],
    }

    async def via_registry_and_mcp() -> tuple[dict, dict]:
        database = Database(min_size=1, max_size=1)
        await database.open()
        try:
            registry = build_registry(database)
            direct = await registry.get("fred").handler(series="VIXCLS", limit=2)
            again = await registry.get("fred").handler(series="VIXCLS", limit=2)
            assert again == direct
            async with Client(build_mcp(registry)) as mcp_client:
                tools = await mcp_client.list_tools()
                assert "get_fred" in {t.name for t in tools.tools}
                call = await mcp_client.call_tool("get_fred", {"series": "VIXCLS", "limit": 2})
            payload = call.structured_content
            via_mcp = payload if "series" in payload else payload["result"]
            return direct, via_mcp
        finally:
            await database.close()

    via_registry, via_mcp = asyncio.run(via_registry_and_mcp())
    assert built == ["fred-key"]  # one client per registry, shared TTL cache

    with TestClient(create_app(settings)) as tc:
        via_api = tc.get("/fred", params={"series": "VIXCLS", "limit": 2}).json()
        assert tc.get("/fred").json()["series"] == "VIXCLS"  # defaults: VIXCLS, 10

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(cli.app, ["fred", "latest", "--series", "VIXCLS", "--limit", "2"])
    assert result.exit_code == 0, result.output
    via_cli = json.loads(result.output)

    assert via_registry == via_api == via_mcp == via_cli == expected
