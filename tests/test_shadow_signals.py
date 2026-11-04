"""Shadow signals (#144): the Kronos forecast and crypto funding/OI, scored before any
Bot reads them.

Both are read on every decide of a Bot that subscribes, fetched on use (the Kronos
sidecar over HTTP, ccxt public endpoints) and journaled in ``decisions.signals``. The
decide sees neither unless the subscription says ``in_prompt`` (off by default, flipped
by its own audited command). The sidecar being down changes nothing but the journal.
The scorer reads the journal back against later bars: Kronos band coverage and hit rate,
funding/OI rank IC.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

from tests.test_loop import BTC, HOLD, NOW, SPY, StubMarketData, build_runner
from tests.test_supervisor import _reset_store, bot_two, settings
from trader.core.types import Bar
from trader.data.derivatives import DerivativesClient, DerivativesError
from trader.data.kronos import KronosClient, KronosError
from trader.eval.signals import forward_return, rank_ic, score_readings, score_signals
from trader.loop.bots import BOT_ONE, BotDesk, BotSpecError, bot_one_spec, bot_spec
from trader.loop.context import ContextBuilder
from trader.loop.journal import Journal
from trader.loop.sources import (
    CRYPTO_DERIVATIVES,
    IN_PROMPT,
    KRONOS,
    CryptoDerivativesSource,
    KronosSource,
)
from trader.store.db import Database

HORIZON = 4


def hourly(closes: list[float], *, end: datetime = NOW, symbol: str = "BTC/USD") -> list[Bar]:
    """Closed 1h bars, the last one opening an hour before ``end``."""
    n = len(closes)
    return [
        Bar(symbol, "1h", end - timedelta(hours=n - i), c, c * 1.01, c * 0.99, c, 10.0)
        for i, c in enumerate(closes)
    ]


class BarsData(StubMarketData):
    """Serves 100 rising hourly bars ending at NOW; with ``later``, also one bar at NOW
    that a pinned read must never pass to the model."""

    def __init__(self, *, later: bool = False) -> None:
        super().__init__([100.0 + i for i in range(100)])
        self.later = later

    async def get_bars(self, symbol, interval, lookback):
        self.bar_calls += 1
        out = hourly(self._series, symbol=symbol.ticker)
        if self.later:
            out.append(Bar(symbol.ticker, "1h", NOW, 999.0, 999.0, 999.0, 999.0, 1.0))
        return out[-lookback:]


class FakeKronos:
    """A KronosClient stand-in: records the bars it was sent, answers a fixed summary."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[list[Bar]] = []

    async def forecast(self, symbol, bars):
        self.sent.append(bars)
        if self.fail:
            raise KronosError("kronos sidecar: connection refused")
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=SIDECAR_REPLY))
        client = KronosClient(
            "http://kronos", paths=4, horizon_bars=HORIZON, client=_client(transport)
        )
        return await client.forecast(symbol, bars)


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://kronos", transport=transport)


# Four paths ending at +10%, +5%, -2% and +1% of the last close (199).
SIDECAR_REPLY = {
    "model": "NeoQuasar/Kronos-small",
    "revision": "abc123",
    "inference_ms": 412,
    "paths": [
        [199.0, 200.0, 210.0, 218.9],
        [199.0, 199.0, 205.0, 208.95],
        [199.0, 198.0, 197.0, 195.02],
        [199.0, 199.5, 200.0, 200.99],
    ],
}


class FakeExchange:
    def __init__(self, venue: str, *, funding: float, fail: bool = False) -> None:
        self.venue = venue
        self.funding = funding
        self.fail = fail
        self.closed = False

    async def fetch_funding_rate(self, market):
        if self.fail:
            raise RuntimeError(f"{self.venue} 451")
        assert market == "BTC/USDT:USDT"
        return {"fundingRate": self.funding, "markPrice": 60_060.0, "indexPrice": 60_000.0}

    async def fetch_open_interest(self, market):
        return {"openInterestAmount": 100.0, "openInterestValue": None}

    async def fetch_open_interest_history(self, market, timeframe, limit=None):
        return [
            {"timestamp": 2, "openInterestValue": 110.0},
            {"timestamp": 1, "openInterestValue": 100.0},
        ]

    async def close(self):
        self.closed = True


def derivatives(*, okx_fails: bool = False) -> DerivativesClient:
    exchanges = {
        "binance": FakeExchange("binance", funding=0.0001),
        "bybit": FakeExchange("bybit", funding=0.0003),
        "okx": FakeExchange("okx", funding=0.0005, fail=okx_fails),
    }
    return DerivativesClient(exchanges, exchange_factory=exchanges.__getitem__)


def signal_sources(kronos=None, derivs=None) -> dict:
    return {
        KRONOS: KronosSource(kronos if kronos is not None else FakeKronos()),
        CRYPTO_DERIVATIVES: CryptoDerivativesSource(derivs or derivatives()),
    }


def subs(in_prompt: bool | None = None) -> list[dict]:
    params = {} if in_prompt is None else {IN_PROMPT: in_prompt}
    return [{"source": KRONOS, "params": params}, {"source": CRYPTO_DERIVATIVES, "params": params}]


# --- the clients -----------------------------------------------------------------------


async def test_kronos_client_sends_the_bars_and_summarizes_every_path():
    seen: list[dict] = []

    def sidecar(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=SIDECAR_REPLY)

    client = KronosClient(
        "http://kronos", paths=4, horizon_bars=HORIZON, client=_client(httpx.MockTransport(sidecar))
    )
    bars = hourly([100.0 + i for i in range(100)])
    forecast = await client.forecast("BTC/USD", bars)

    body = seen[0]
    assert (body["symbol"], body["horizon"], body["paths"], len(body["bars"])) == (
        "BTC/USD", HORIZON, 4, 100,
    )  # fmt: skip
    assert body["bars"][-1] == {
        "ts": bars[-1].ts.isoformat(), "open": 199.0, "high": 200.99, "low": 197.01,
        "close": 199.0, "volume": 10.0,
    }  # fmt: skip
    # Not averaged: three of four paths end up; the band spans the sampled ends.
    assert forecast.share_up == 0.75
    assert forecast.median == pytest.approx(0.03)
    assert forecast.p10 == pytest.approx(-0.011)
    assert forecast.p90 == pytest.approx(0.085)
    assert (forecast.model, forecast.revision, forecast.inference_ms) == (
        "NeoQuasar/Kronos-small", "abc123", 412,
    )  # fmt: skip
    assert forecast.last_close == 199.0 and forecast.last_bar_ts == bars[-1].ts


@pytest.mark.parametrize(
    "answer",
    [
        lambda r: httpx.Response(503, text="model loading"),
        lambda r: httpx.Response(200, json={"paths": [[1.0]]}),  # wrong horizon
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    ],
)
async def test_kronos_client_refuses_an_unusable_answer(answer):
    client = KronosClient(
        "http://kronos", paths=4, horizon_bars=HORIZON, client=_client(httpx.MockTransport(answer))
    )
    with pytest.raises(KronosError):
        await client.forecast("BTC/USD", hourly([100.0] * 80))


async def test_derivatives_reads_every_venue_and_drops_one_that_fails():
    snap = await derivatives(okx_fails=True).snapshot("BTC")
    assert [v.venue for v in snap.venues] == ["binance", "bybit"]
    assert snap.funding_rate == pytest.approx(0.0002)
    assert snap.basis == pytest.approx(0.001)
    assert snap.oi_change_24h == pytest.approx(0.10)
    assert snap.venues[0].open_interest_usd == pytest.approx(100.0 * 60_060.0)


async def test_derivatives_with_no_venue_answering_is_an_error():
    exchanges = {"okx": FakeExchange("okx", funding=0.0, fail=True)}
    client = DerivativesClient(["okx"], exchange_factory=exchanges.__getitem__)
    with pytest.raises(DerivativesError):
        await client.snapshot("BTC")
    with pytest.raises(DerivativesError):
        await client.snapshot("SOL")  # no perp reading for other bases
    await client.close()
    assert exchanges["okx"].closed


# --- the Sources ------------------------------------------------------------------------


async def test_a_pinned_kronos_read_sends_only_bars_before_as_of():
    kronos = FakeKronos()
    source = KronosSource(kronos)
    piece = await source.read(BTC, as_of=NOW, params={"market_data": BarsData(later=True)})
    assert piece is not None
    assert max(b.ts for b in kronos.sent[0]) < NOW
    assert piece.detail["share_up"] == 0.75
    assert "75% of 4 sampled paths end up after 4 x 1h bars" in piece.text
    # No client, or no bars provider: no reading, no error.
    assert await KronosSource(None).read(BTC, as_of=None, params={}) is None
    assert await source.read(BTC, as_of=None, params={}) is None


async def test_derivatives_read_live_crypto_only():
    source = CryptoDerivativesSource(derivatives())
    piece = await source.read(BTC, as_of=None, params={"now": NOW})
    assert piece is not None and piece.as_of == NOW
    assert piece.text.startswith("BTC perpetuals (binance, bybit, okx): funding rate +0.0300%")
    # The venues keep no point-in-time history we read: a replay step gets nothing.
    assert await source.read(BTC, as_of=NOW, params={}) is None
    assert await source.read(SPY, as_of=None, params={"now": NOW}) is None


# --- the decide context -----------------------------------------------------------------


async def build(sources: dict, subscriptions: list[dict]):
    builder = ContextBuilder(BarsData(), sources=sources)
    return await builder.build(BTC, now=NOW, triggers=[], subscriptions=subscriptions)


async def test_by_default_the_signals_are_journaled_and_kept_out_of_the_prompt():
    ctx = await build(signal_sources(), subs())
    prompt = ctx.to_prompt()
    assert "Kronos" not in prompt and "perpetuals" not in prompt
    assert ctx.source_versions == {}  # the decide read neither
    assert set(ctx.signals) == {KRONOS, CRYPTO_DERIVATIVES}
    kronos = ctx.signals[KRONOS]
    assert kronos["in_prompt"] is False
    assert kronos["reading"]["p90"] == pytest.approx(0.085)
    assert ctx.signals[CRYPTO_DERIVATIVES]["reading"]["funding_rate"] == pytest.approx(0.0003)


async def test_in_prompt_puts_each_signal_in_its_own_advisory_lane():
    ctx = await build(signal_sources(), subs(in_prompt=True))
    prompt = ctx.to_prompt()
    assert "Kronos forecast (advisory" in prompt
    assert "Crypto derivatives (advisory" in prompt
    assert set(ctx.source_versions) == {KRONOS, CRYPTO_DERIVATIVES}
    assert ctx.signals[KRONOS]["in_prompt"] is True
    assert ctx.signals[KRONOS]["version"] == ctx.source_versions[KRONOS]["version"]


async def test_the_sidecar_down_drops_only_its_reading():
    ctx = await build(signal_sources(kronos=FakeKronos(fail=True)), subs(in_prompt=True))
    assert set(ctx.signals) == {CRYPTO_DERIVATIVES}
    assert "Kronos" not in ctx.to_prompt()


async def test_every_decide_journals_its_signals(db: Database):
    runner = build_runner(db, [HOLD], market_data=BarsData(), sources=subs())
    runner._context = ContextBuilder(runner.market_data, sources=signal_sources())
    results = await runner.tick(NOW, force=True)
    row = await Journal(db).get(results[0].decision_id)
    assert set(row["signals"]) == {KRONOS, CRYPTO_DERIVATIVES}
    assert row["signals"][KRONOS]["reading"]["model"] == "NeoQuasar/Kronos-small"
    assert "Kronos" not in row["prompt"]

    # A Bot that subscribes to neither journals no signals at all.
    plain = build_runner(db, [HOLD], market_data=BarsData())
    results = await plain.tick(NOW, force=True)
    assert (await Journal(db).get(results[0].decision_id))["signals"] is None


# --- the Bot spec and its toggle --------------------------------------------------------


def test_bot_one_subscribes_journal_only_behind_its_setting():
    assert {s["source"] for s in bot_one_spec(settings())["sources"]} & {KRONOS} == set()
    on = bot_one_spec(settings(shadow_signals_enabled=True))["sources"]
    assert {"source": KRONOS, "params": {}} in on
    assert {"source": CRYPTO_DERIVATIVES, "params": {}} in on


def test_a_spec_refuses_a_non_boolean_in_prompt():
    with pytest.raises(BotSpecError, match="in_prompt must be true or false"):
        bot_spec(
            symbols={"BTC/USD": "shadow"},
            capital_usd=1_000.0,
            sources=[{"source": KRONOS, "params": {IN_PROMPT: "yes"}}],
        )


async def test_the_toggle_is_a_new_version_and_off_drops_the_setting(db: Database):
    desk = BotDesk(db)
    await desk.seed(BOT_ONE, bot_one_spec(settings()))
    await bot_two(db, sources=[KRONOS, "brief"])

    on = await desk.set_signal_prompt("bot-2", KRONOS, in_prompt=True)
    assert on["version"] == 2
    assert on["spec"]["sources"][0] == {"source": KRONOS, "params": {IN_PROMPT: True}}
    off = await desk.set_signal_prompt("bot-2", KRONOS, in_prompt=False)
    assert off["version"] == 3 and off["spec"]["sources"][0] == {"source": KRONOS, "params": {}}
    audit = await db.fetch_all("SELECT action, payload FROM audit_events ORDER BY id")
    assert [r["payload"].get(IN_PROMPT) for r in audit if r["action"] == "set_signal_prompt"] == [
        True, False,
    ]  # fmt: skip

    with pytest.raises(BotSpecError, match="bot-1 follows Settings"):
        await desk.set_signal_prompt(BOT_ONE, KRONOS, in_prompt=True)
    with pytest.raises(BotSpecError, match="not a shadow signal"):
        await desk.set_signal_prompt("bot-2", "brief", in_prompt=True)
    with pytest.raises(BotSpecError, match="does not read crypto_derivatives"):
        await desk.set_signal_prompt("bot-2", CRYPTO_DERIVATIVES, in_prompt=True)


# --- scoring ----------------------------------------------------------------------------


def test_forward_return_counts_bars_and_waits_for_the_horizon_to_close():
    series = hourly([100.0, 101.0, 102.0, 103.0, 104.0])
    assert forward_return(series, series[0].ts, 2) == pytest.approx(0.02)
    assert forward_return(series, series[0].ts, 2, base=50.0) == pytest.approx(102 / 50 - 1)
    assert forward_return(series, series[3].ts, 2) is None
    assert forward_return(series, series[0].ts - timedelta(hours=1), 1) is None


def test_rank_ic():
    assert rank_ic([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert rank_ic([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert rank_ic([1, 2], [1, 2]) is None
    assert rank_ic([1, 1, 1], [1, 2, 3]) is None


def kronos_row(series: list[Bar], i: int, *, p10: float, p90: float, share_up: float) -> dict:
    reading = {
        "last_bar_ts": series[i].ts.isoformat(), "last_close": series[i].close,
        "p10": p10, "p90": p90, "share_up": share_up,
    }  # fmt: skip
    return {"symbol": "BTC/USD", "signals": {KRONOS: {"reading": reading}}}


def test_kronos_band_coverage_and_hit_rate():
    # +1% a bar: every 2-bar forward return is about +2%.
    series = hourly([100.0 * 1.01**i for i in range(10)])
    rows = [
        kronos_row(series, 0, p10=0.0, p90=0.05, share_up=0.8),  # inside, right
        kronos_row(series, 1, p10=0.03, p90=0.05, share_up=0.3),  # outside, wrong
        kronos_row(series, 1, p10=0.03, p90=0.05, share_up=0.3),  # the same bar again
        kronos_row(series, 2, p10=-0.05, p90=0.05, share_up=0.5),  # inside, no call
        kronos_row(series, 9, p10=-0.05, p90=0.05, share_up=0.9),  # horizon still open
    ]
    out = score_readings(rows, {"BTC/USD": series}, horizon=2)
    assert out["pending"] == 1
    assert out[KRONOS]["overall"] == {"n": 3, "coverage": pytest.approx(2 / 3), "hit_rate": 0.5}
    assert out[KRONOS]["by_symbol"]["BTC/USD"]["n"] == 3


def test_funding_rank_ic_against_the_forward_return():
    closes = [100.0, 101.0, 103.0, 106.0, 110.0, 115.0, 121.0]  # accelerating
    series = hourly(closes)
    rows = []
    for i in range(5):
        as_of = (series[i].ts + timedelta(hours=1)).isoformat()
        reading = {"funding_rate": 0.0001 * i, "oi_change_24h": -0.01 * i, "basis": None}
        rows.append(
            {
                "symbol": "BTC/USD",
                "signals": {CRYPTO_DERIVATIVES: {"as_of": as_of, "reading": reading}},
            }
        )
    out = score_readings(rows, {"BTC/USD": series}, horizon=2)
    pooled = out[CRYPTO_DERIVATIVES]["pooled"]
    assert pooled["n"] == 5
    assert pooled["funding_rate"]["ic"] == pytest.approx(1.0)
    assert pooled["oi_change_24h"]["ic"] == pytest.approx(-1.0)
    assert pooled["basis"] == {"n": 0, "ic": None}


async def test_score_signals_reads_the_journal_and_fetches_bars_on_use(db: Database):
    runner = build_runner(db, [HOLD], market_data=BarsData(), sources=subs())
    runner._context = ContextBuilder(runner.market_data, sources=signal_sources())
    await runner.tick(NOW, force=True)

    registry = settings().symbol_registry()
    # Scored the same hour: the horizon has not closed, so both readings wait.
    out = await score_signals(db, BarsData(), registry, days=14, horizon=HORIZON, now=NOW)
    assert (out["readings"], out["pending"]) == (1, 2)

    class Later(StubMarketData):
        """The same rising bars, now running 40h past NOW (the last 16 still open)."""

        def __init__(self) -> None:
            super().__init__()
            self.bar_calls = 0

        async def get_bars(self, symbol, interval, lookback):
            self.bar_calls += 1
            return hourly([100.0 + i for i in range(140)], end=NOW + timedelta(hours=40))

    data = Later()
    out = await score_signals(
        db, data, registry, days=14, horizon=HORIZON, now=NOW + timedelta(days=1)
    )
    assert out["pending"] == 0
    assert data.bar_calls == 1  # one fetch per symbol
    # From 199 at the last bar Kronos saw to 203 four bars on: inside p10..p90, and up.
    assert out[KRONOS]["overall"] == {"n": 1, "coverage": 1.0, "hit_rate": 1.0}
    assert out[CRYPTO_DERIVATIVES]["pooled"]["n"] == 1


# --- every door -------------------------------------------------------------------------

CREATE = {
    "name": "bot-2-kronos",
    "symbols": {"ETH/USD": "shadow"},
    "capital_usd": 5_000.0,
    "sources": [KRONOS, "brief"],
    "prompt_variant": "equity-largecap",
}


def test_the_toggle_and_the_scores_answer_on_every_door(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    boot = settings(loop_symbols="BTC/USD")
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(boot)))
    body = {"source": KRONOS, "in_prompt": True}

    def via_api() -> dict:
        with TestClient(create_app(boot)) as tc:
            assert tc.post("/bots", json=CREATE).status_code == 200
            out = tc.post("/bots/bot-2-kronos/signal-prompt", json=body).json()
            refused = tc.post("/bots/bot-1/signal-prompt", json=body)
            assert refused.status_code == 422 and "bot-1 follows Settings" in refused.text
            # No loop in this build, so no bars provider: the scorer says so.
            scores = tc.get("/signals/scores", params={"days": 7}).json()
            assert scores["status"] == "unavailable"
        return out

    async def via_mcp() -> dict:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            await BotDesk(db).seed(BOT_ONE, bot_one_spec(boot))
            async with Client(build_mcp(build_registry(db))) as mcp_client:
                made = await mcp_client.call_tool("create_bot", CREATE)
                assert not made.is_error, made.content
                result = await mcp_client.call_tool(
                    "set_signal_prompt", {"name": "bot-2-kronos", **body}
                )
                assert not result.is_error, result.content
                scores = await mcp_client.call_tool("get_signal_scores", {"days": 7})
                assert not scores.is_error and "unavailable" in str(scores.content)
            return result.structured_content
        finally:
            await db.close()

    def via_cli() -> dict:
        made = CliRunner().invoke(
            cli.app,
            [
                "bot", "create", "bot-2-kronos", "--symbols", "ETH/USD", "--capital", "5000",
                "--source", KRONOS, "--source", "brief", "--prompt", "equity-largecap",
            ],
        )  # fmt: skip
        assert made.exit_code == 0, made.output
        printed = CliRunner().invoke(
            cli.app, ["bot", "signal", "bot-2-kronos", KRONOS, "--in-prompt"]
        )
        assert printed.exit_code == 0, printed.output
        scores = CliRunner().invoke(cli.app, ["signal-scores", "--days", "7"])
        assert scores.exit_code == 0 and "unavailable" in scores.output
        return json.loads(printed.output)

    api = via_api()
    _reset_store()
    mcp = asyncio.run(via_mcp())
    _reset_store()
    printed = via_cli()

    assert api == mcp == printed
    assert api["version"] == 2
    assert api["spec"]["sources"][0] == {"source": KRONOS, "params": {IN_PROMPT: True}}


def test_the_risk_package_never_imports_the_signal_clients():
    import subprocess
    import sys

    code = (
        "import sys, trader.risk.guard; "
        "print(any(m in sys.modules for m in ('trader.data.kronos', 'trader.data.derivatives')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
