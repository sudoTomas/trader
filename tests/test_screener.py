"""#5 — the S&P 100 screener: a bounded shortlist per query, on all three surfaces.

The ranker is pure (synthetic bars in, ranked candidates out); the multi-symbol fetch
runs over ``httpx.MockTransport`` like ``tests/test_data.py``; the API route and the CLI
run over a stubbed provider. Nothing here touches Alpaca or stores market data.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trader.core.types import Bar
from trader.data.alpaca import AlpacaData
from trader.data.universe import SP100, universe
from trader.research.screener import Candidate, ScreenResult, rank_universe

# --- synthetic bars ------------------------------------------------------------

T0 = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)


def daily_bars(symbol: str, closes: list[float], volume: float = 1_000_000.0) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            interval="1d",
            ts=T0 + timedelta(days=i),
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            volume=volume,
        )
        for i, c in enumerate(closes)
    ]


def trending(start: float, daily_return: float, n: int = 61) -> list[float]:
    return [start * (1 + daily_return) ** i for i in range(n)]


# --- universe ------------------------------------------------------------------


def test_sp100_is_a_static_deduplicated_upper_case_list():
    assert len(SP100) >= 100
    assert len(set(SP100)) == len(SP100)
    assert all(t == t.upper() and " " not in t for t in SP100)
    assert {"AAPL", "MSFT", "NVDA", "BRK.B"} <= set(SP100)
    assert universe("sp100") == SP100
    assert universe("SP100") == SP100  # case-insensitive lookup
    with pytest.raises(KeyError):
        universe("russell3000")


# --- ranker --------------------------------------------------------------------


def test_rank_orders_by_risk_adjusted_momentum_and_reports_metrics():
    bars = {
        "UP": daily_bars("UP", trending(100, 0.005)),  # steady riser, low vol
        "FLAT": daily_bars("FLAT", trending(100, 0.0)),  # no momentum
        "DOWN": daily_bars("DOWN", trending(100, -0.004)),
    }
    result = rank_universe("test", bars, exclude=set(), min_dollar_volume=0.0, limit=10)

    assert isinstance(result, ScreenResult)
    assert [c.symbol for c in result.shortlist] == ["UP", "FLAT", "DOWN"]
    up = result.shortlist[0]
    assert isinstance(up, Candidate)
    assert up.momentum_20d == pytest.approx(1.005**20 - 1)
    assert up.momentum_60d == pytest.approx(1.005**60 - 1)
    assert up.close == pytest.approx(100 * 1.005**60)
    assert up.dollar_volume_20d == pytest.approx(
        sum(b.close * b.volume for b in bars["UP"][-20:]) / 20
    )
    assert up.realized_vol_20d >= 0.0 and math.isfinite(up.score)
    assert "20d" in up.reason and "60d" in up.reason
    flat = result.shortlist[1]
    assert flat.momentum_20d == pytest.approx(0.0) and flat.score == pytest.approx(0.0)
    assert result.considered == 3 and result.eligible == 3


def test_rank_filters_excluded_symbols_illiquid_names_and_short_histories():
    bars = {
        "SPY": daily_bars("SPY", trending(100, 0.003)),  # already traded by the loop
        "THIN": daily_bars("THIN", trending(50, 0.004), volume=100.0),  # illiquid
        "NEW": daily_bars("NEW", trending(10, 0.01, n=30)),  # too little history
        "OK": daily_bars("OK", trending(100, 0.002)),
    }
    result = rank_universe("test", bars, exclude={"SPY"}, min_dollar_volume=1_000_000.0, limit=10)

    assert [c.symbol for c in result.shortlist] == ["OK"]
    assert result.excluded == ["SPY"]
    assert "THIN" in result.skipped and "dollar volume" in result.skipped["THIN"]
    assert "NEW" in result.skipped and "bars" in result.skipped["NEW"]
    assert result.considered == 4 and result.eligible == 1


def test_rank_bounds_the_shortlist_and_is_deterministic():
    bars = {f"S{i:02d}": daily_bars(f"S{i:02d}", trending(100, 0.001 * (i % 7))) for i in range(30)}
    first = rank_universe("test", bars, exclude=set(), min_dollar_volume=0.0, limit=5)
    second = rank_universe(
        "test", dict(reversed(list(bars.items()))), exclude=set(), min_dollar_volume=0.0, limit=5
    )

    assert len(first.shortlist) == 5
    assert first.as_dict() == second.as_dict()  # input order never changes the answer
    scores = [c.score for c in first.shortlist]
    assert scores == sorted(scores, reverse=True)
    # Ties (identical synthetic series) break on the symbol, never on dict order.
    tied = [c.symbol for c in first.shortlist if c.score == scores[0]]
    assert tied == sorted(tied)
    assert json.dumps(first.as_dict())  # JSON-clean for every surface


def test_rank_with_no_eligible_symbols_returns_an_empty_shortlist():
    result = rank_universe("test", {}, exclude=set(), min_dollar_volume=0.0, limit=10)
    assert result.shortlist == [] and result.considered == 0
    assert result.as_dict()["shortlist"] == []


# --- multi-symbol daily bars over MockTransport ----------------------------------


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")


def _row(day: int, close: float) -> dict:
    return {
        "t": f"2026-08-{day:02d}T04:00:00Z",
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": 10,
    }


async def test_multi_symbol_daily_bars_batch_paginate_and_trim():
    """One request per batch (URL under ~2k chars), next_page_token followed, per-symbol
    ascending lists trimmed to the lookback. sip feed + a delayed end, like get_bars
    (docs/lessons.md#alpaca-pagination-cap, #alpaca-start-default-caps-to-today)."""
    calls: list[dict] = []
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        urls.append(str(request.url))
        symbols = params["symbols"].split(",")
        if "AAA" in symbols and "page_token" not in params:
            return httpx.Response(
                200,
                json={
                    "bars": {"AAA": [_row(1, 1.0), _row(2, 2.0)], "BBB": [_row(1, 10.0)]},
                    "next_page_token": "PAGE-2",
                },
            )
        if "AAA" in symbols:
            assert params["page_token"] == "PAGE-2"
            return httpx.Response(
                200,
                json={
                    "bars": {"AAA": [_row(3, 3.0)], "BBB": [_row(2, 20.0), _row(3, 30.0)]},
                    "next_page_token": None,
                },
            )
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})

    data = AlpacaData("k", "s", client=mock_client(handler))
    # ~80 padding symbols of 20 chars force at least two batches under a ~2k-char URL.
    padding = [f"PAD{i:017d}" for i in range(80)]
    out = await data.get_daily_bars_multi(["AAA", "BBB", *padding], lookback_days=2)

    assert all(len(u) < 2048 for u in urls)
    assert len({c["symbols"] for c in calls}) >= 2  # batched
    first = calls[0]
    assert first["timeframe"] == "1Day" and first["feed"] == "sip"
    assert first["adjustment"] == "split" and "start" in first
    end = datetime.fromisoformat(first["end"])
    assert timedelta(minutes=15) <= datetime.now(tz=UTC) - end <= timedelta(minutes=30)
    assert [b.close for b in out["AAA"]] == [2.0, 3.0]  # trimmed to the last 2, ascending
    assert [b.close for b in out["BBB"]] == [20.0, 30.0]
    assert all(b.interval == "1d" and b.ts.tzinfo is not None for b in out["AAA"])
    assert out["PAD00000000000000000"] == []  # requested, nothing traded: still a key


async def test_multi_symbol_daily_bars_with_no_symbols_makes_no_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected")

    data = AlpacaData("k", "s", client=mock_client(handler))
    assert await data.get_daily_bars_multi([], lookback_days=5) == {}


# --- surfaces: registry, API, CLI, MCP over a stubbed provider --------------------


class StubScreenerData:
    """Serves synthetic daily bars for whatever symbols it is asked for; records calls."""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def get_daily_bars_multi(self, symbols, lookback_days: int) -> dict[str, list[Bar]]:
        self.calls.append((tuple(symbols), lookback_days))
        out: dict[str, list[Bar]] = {}
        for i, symbol in enumerate(symbols):
            drift = 0.004 if symbol == "AAPL" else 0.001 * (i % 5)
            out[symbol] = daily_bars(symbol, trending(100, drift, n=lookback_days))
        return out


def test_screener_query_on_registry_api_cli_and_mcp(clean_db: None, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.ops.config as config_module
    import trader.surfaces.api.app as app_module
    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    stub = StubScreenerData()
    settings = Settings(auth_disabled=True, loop_symbols="BTC/USD,AAPL")
    # The handler reads the process singleton (like risk_status); point it at this build.
    monkeypatch.setattr(config_module, "_settings", settings)
    monkeypatch.setattr(app_module, "build_screener_data", lambda db, settings: stub)

    async def via_registry_and_mcp():
        registry = build_registry(None, screener=stub)
        cap = registry.get("screener")
        assert cap.kind == "query" and cap.tool_name == "get_screener"
        direct = await cap.handler(universe="sp100", limit=3)
        async with Client(build_mcp(registry)) as mcp_client:
            tools = await mcp_client.list_tools()
            assert "get_screener" in {t.name for t in tools.tools}
            call = await mcp_client.call_tool("get_screener", {"universe": "sp100", "limit": 3})
        return direct, call.structured_content

    direct, via_mcp = asyncio.run(via_registry_and_mcp())

    assert direct["universe"] == "sp100" and direct["considered"] == len(SP100)
    assert len(direct["shortlist"]) == 3
    assert "AAPL" not in [c["symbol"] for c in direct["shortlist"]]  # loop symbol excluded
    assert direct["excluded"] == ["AAPL"]
    assert stub.calls[0][0] == SP100 and stub.calls[0][1] == settings.screener_lookback_days
    assert all(
        {"symbol", "score", "reason", "momentum_20d", "momentum_60d", "realized_vol_20d"} <= set(c)
        for c in direct["shortlist"]
    )
    assert via_mcp == direct

    with TestClient(create_app(settings)) as tc:
        via_api = tc.get("/screener", params={"universe": "sp100", "limit": 3}).json()
        assert tc.get("/screener", params={"universe": "nope"}).status_code == 422
        assert tc.get("/screener", params={"limit": 0}).status_code == 422
    assert via_api == direct

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(cli.app, ["screener", "run", "--universe", "sp100", "--limit", "3"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == direct


def test_screener_without_market_data_credentials_is_honest(clean_db: None):
    import asyncio

    from trader.surfaces.services import build_registry

    registry = build_registry(None)  # no provider in this build (CI has no Alpaca keys)
    out = asyncio.run(registry.get("screener").handler())
    assert out["shortlist"] == []
    assert "credential" in out["reason"]


def test_screener_settings_have_sane_defaults():
    from trader.ops.config import Settings

    settings = Settings(auth_disabled=True)
    assert settings.screener_lookback_days >= 61  # 60d momentum needs 61 closes
    assert settings.screener_min_dollar_volume_usd > 0
