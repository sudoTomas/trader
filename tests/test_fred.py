"""FRED reaches the decide context fetch-on-use: latest print only, nothing stored.

The client is exercised with a mocked transport. Context and replay tests stub
``get_series`` so the suite never calls api.stlouisfed.org.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx

from tests.test_loop import NOW, StubMarketData
from tests.test_replay_eval import HOLD, seed_hourly_bars
from trader.core.symbols import default_registry
from trader.data.base import MarketDataError
from trader.data.composite import CompositeMarketData, build_market_data
from trader.data.fred import Fred
from trader.eval.replay import ReplayHarness
from trader.loop.context import ContextBuilder
from trader.ops.config import Settings
from trader.store.db import Database

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


async def test_context_attaches_the_latest_fred_print_on_or_before_now():
    fred = RecordingFred(_prints())
    ctx = await ContextBuilder(StubMarketData(), fred=fred).build(BTC, now=NOW, triggers=[])

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
    """The live runner builds ContextBuilder(market_data) and does not grow a fred kwarg.
    Composite exposes the client; the builder has to pick it up from there."""
    fred = RecordingFred(_prints())

    class _WithFred(StubMarketData):
        pass

    data = _WithFred()
    data.fred = fred
    ctx = await ContextBuilder(data).build(BTC, now=NOW, triggers=[])
    assert [p.value for p in ctx.fred_series] == [18.2]
    assert fred.calls  # fetched, not a stored window


async def test_context_degrades_when_fred_fails_and_omits_the_lane_when_absent():
    failed = await ContextBuilder(StubMarketData(), fred=BoomFred()).build(
        BTC, now=NOW, triggers=[]
    )
    assert failed.price > 0
    assert failed.fred_series == ()
    assert "VIXCLS" not in failed.to_prompt()

    plain = await ContextBuilder(StubMarketData()).build(BTC, now=NOW, triggers=[])
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
