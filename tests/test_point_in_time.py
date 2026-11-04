"""Point in time (#142): in replay, nothing dated after the step reaches a prompt.

One rule for every Source (``trader.loop.pit``): a pinned read is clamped to ``as_of`` and
a slice whose availability time is later is refused. The look-ahead cases below cover
every Source a Bot may subscribe to (asserted), each with data planted after the step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from tests.source_fixtures import (
    BTC,
    HELD,
    SENTIMENT,
    StubBriefs,
    StubFred,
    StubMacroViews,
    StubSentiment,
)
from tests.test_loop import HOLD, NOW, StubLLM, StubMarketData
from tests.test_replay_eval import seed_hourly_bars
from tests.vendor_fixtures import AAPL, POLY_LATER, POLY_NOW, RETAIL_NOW, StubVendorReads
from trader.core.types import Bar
from trader.data.derivatives import DerivativesSnapshot, VenueReading
from trader.data.kronos import KronosForecast
from trader.eval.blind import split_sources
from trader.eval.replay import ReplayHarness
from trader.loop import pit
from trader.loop.bots import SOURCE_NAMES
from trader.loop.context import ContextBuilder
from trader.loop.sources import (
    CRYPTO_DERIVATIVES,
    IN_PROMPT,
    KRONOS,
    BriefSource,
    CryptoDerivativesSource,
    FredSource,
    HeldPositionSource,
    KronosSource,
    MacroViewSource,
    SentimentSource,
    SourceSlice,
    Subscription,
)
from trader.loop.vendor_sources import (
    VENDOR_SOURCES,
    PolymarketSource,
    RetailSentimentSource,
    SecFilingsSource,
)
from trader.store.db import Database
from trader.store.repos import VendorReadsRepo

# A poll scored after the step: its score must never reach a replayed prompt.
LATE_POLL = {
    "id": 14,
    "symbol": "BTC/USD",
    "status": "ok",
    "score": 0.99,
    "volume": "storm",
    "ts": NOW + timedelta(minutes=30),
}


class EchoKronos:
    """A Kronos client that reports the last close it was sent as its path count, so a
    bar from after the step would show in the prompt as ``999 sampled paths``."""

    async def forecast(self, symbol, bars):
        last = bars[-1]
        return KronosForecast(
            model="stub", revision="r1", interval="1h", horizon_bars=24,
            paths=int(last.close), last_bar_ts=last.ts, last_close=last.close,
            share_up=0.5, median=0.0, p10=-0.01, p90=0.01, inference_ms=1,
        )  # fmt: skip


class StubPerps:
    async def snapshot(self, base):
        venue = VenueReading("okx", 0.0001, 1e9, 0.01, 0.0002)
        return DerivativesSnapshot(base=base, venues=(venue,))


class LateBarData(StubMarketData):
    """100 hourly bars closing by the step, then one opening at it (close 999)."""

    async def get_bars(self, symbol, interval, lookback):
        bars = [
            Bar(symbol.ticker, "1h", NOW - timedelta(hours=100 - i), 100.0, 100.0, 100.0, 100.0, 1)
            for i in range(100)
        ]
        bars.append(Bar(symbol.ticker, "1h", NOW, 999.0, 999.0, 999.0, 999.0, 1.0))
        return bars[-lookback:]


def every_source() -> dict[str, Any]:
    sentiment = SentimentSource(StubSentiment([*SENTIMENT, LATE_POLL], now=NOW + timedelta(days=1)))
    vendor = StubVendorReads()
    return {
        "macro_view": MacroViewSource(StubMacroViews()),
        "brief": BriefSource(StubBriefs()),
        "fred": FredSource(StubFred()),
        "sentiment": sentiment,
        "held_position": HeldPositionSource(sentiment),
        "sec_filings": SecFilingsSource(vendor),
        "polymarket": PolymarketSource(vendor),
        "retail_sentiment": RetailSentimentSource(vendor),
        KRONOS: KronosSource(EchoKronos()),
        CRYPTO_DERIVATIVES: CryptoDerivativesSource(StubPerps()),
    }


# --- the rule -------------------------------------------------------------------------


def test_a_request_past_as_of_is_clamped_and_a_live_request_passes():
    assert pit.clamp(NOW + timedelta(days=1), NOW) == NOW
    assert pit.clamp(None, NOW) == NOW
    assert pit.clamp(NOW - timedelta(hours=1), NOW) == NOW - timedelta(hours=1)
    assert pit.clamp(NOW + timedelta(days=1), None) == NOW + timedelta(days=1)


def test_only_items_available_by_as_of_are_visible_and_undated_ones_are_withheld():
    assert pit.visible(NOW, NOW) and pit.visible(NOW - timedelta(seconds=1), NOW)
    assert not pit.visible(NOW + timedelta(seconds=1), NOW)
    assert not pit.visible(None, NOW)  # cannot prove it is not from later
    assert pit.visible(None, None) and pit.visible(NOW + timedelta(days=9), None)  # live


def test_a_daily_print_is_available_only_once_its_day_has_ended():
    ended = pit.day_end(date(2026, 8, 11))
    assert ended == datetime.fromisoformat("2026-08-12T00:00:00-04:00")
    assert not pit.visible(ended, NOW - timedelta(days=1))  # 2026-08-11 15:00 UTC
    assert pit.visible(ended, NOW)


# --- the guard ------------------------------------------------------------------------


@dataclass
class Rogue:
    """A Source that ignores ``as_of``: it serves a slice dated an hour after the step."""

    name: str = "macro_view"
    seen: list[dict[str, Any]] = field(default_factory=list)

    async def read(self, symbol, *, as_of, params):
        self.seen.append(dict(params))
        late = NOW + timedelta(hours=1)
        return SourceSlice(self.name, 99, as_of or late, "FUTURE view", available_at=late)


async def test_the_builder_refuses_a_slice_dated_after_the_step_and_clamps_the_clock():
    rogue = Rogue()
    builder = ContextBuilder(
        StubMarketData(), sources={"macro_view": rogue}, subscriptions=[Subscription("macro_view")]
    )
    pinned = await builder.build(BTC, now=NOW + timedelta(days=1), triggers=[], as_of=NOW)
    assert pinned.source_versions == {} and "FUTURE" not in pinned.to_prompt()
    assert rogue.seen[-1]["now"] == NOW  # a clock that ran ahead is served as of the step

    live = await builder.build(BTC, now=NOW, triggers=[])
    assert live.source_versions["macro_view"]["version"] == 99  # live reads are not clamped


async def test_an_undated_slice_is_withheld_from_replay():
    class Undated:
        name = "brief"

        async def read(self, symbol, *, as_of, params):
            return SourceSlice(self.name, 1, NOW, "undated")

    builder = ContextBuilder(StubMarketData(), sources={"brief": Undated()})
    ctx = await builder.build(
        BTC, now=NOW, triggers=[], as_of=NOW, subscriptions=[Subscription("brief")]
    )
    assert ctx.source_versions == {}


# --- look-ahead, one case per Source --------------------------------------------------


@dataclass(frozen=True)
class Case:
    symbol: Any
    version: Any  # what the step may read
    future: tuple[str, ...]  # what it must not
    params: dict[str, Any] = field(default_factory=dict)  # the subscription's
    lane: bool = True  # False: replay reads nothing, a live read does (asserted)


LOOKAHEAD: dict[str, Case] = {
    "macro_view": Case(BTC, 2, ("Later view", "Later crypto")),
    "brief": Case(BTC, 3, ("Later brief",)),
    # The step is 15:00 on 08-12: that day's VIX close is not out yet.
    "fred": Case(BTC, "VIXCLS@2026-08-11", ("424242", "18.2")),
    "sentiment": Case(BTC, 12, ("+0.99",)),
    "held_position": Case(BTC, None, ("+0.99",)),
    "sec_filings": Case(AAPL, 7, ("10-Q/A", "424.24B")),
    "polymarket": Case(BTC, 3, ("FUTURE",)),
    "retail_sentiment": Case(BTC, 5, ("999",)),
    # Shadow signals (#144), subscribed into the prompt so the lane can be checked.
    KRONOS: Case(BTC, None, ("999 sampled",), params={IN_PROMPT: True}),
    # No point-in-time history at the venues: a replay step reads nothing at all.
    CRYPTO_DERIVATIVES: Case(BTC, None, (), params={IN_PROMPT: True}, lane=False),
}


def test_every_subscribable_source_has_a_lookahead_case():
    assert set(LOOKAHEAD) == SOURCE_NAMES


@pytest.mark.parametrize("name", sorted(LOOKAHEAD))
async def test_nothing_dated_after_as_of_reaches_a_replayed_prompt(name: str):
    case = LOOKAHEAD[name]
    data = LateBarData() if name == KRONOS else StubMarketData()  # bars only Kronos reads
    builder = ContextBuilder(data, sources=every_source())
    read = {
        "now": NOW,
        "triggers": [],
        "position_qty": 0.5,
        "position": HELD,
        "subscriptions": [Subscription(name, case.params)],
    }
    ctx = await builder.build(case.symbol, as_of=NOW, **read)
    if not case.lane:
        assert name not in ctx.source_versions and name not in ctx.signals
        live = await builder.build(case.symbol, **read)
        assert name in live.source_versions  # a live read does serve it: not vacuous
        return
    assert name in ctx.source_versions  # the lane is there: the check is not vacuous
    if case.version is not None:
        assert ctx.source_versions[name]["version"] == case.version
    prompt = ctx.to_prompt()
    for marker in case.future:
        assert marker not in prompt, marker
    assert all(piece.available_at <= NOW for piece in ctx.sources)


async def test_sec_filings_serve_the_vintage_current_at_each_step():
    source = SecFilingsSource(StubVendorReads())
    at_step = await source.read(AAPL, as_of=NOW, params={"now": NOW})
    assert at_step is not None
    assert "Revenue: 94.00B USD (quarter ended 2026-06-27; 10-Q filed 2026-07-31)" in at_step.text
    assert "Filing: 8-K filed 2026-08-11 (items 2.02,9.01)" in at_step.text
    assert "Total Assets: 350.00B USD (at 2026-06-27" in at_step.text

    # After the amendment's acceptance the restated figure is the one on file.
    later = NOW + timedelta(hours=4)
    after = await source.read(AAPL, as_of=later, params={"now": later})
    assert after is not None and "Revenue: 424.24B USD" in after.text
    assert "Filing: 10-Q/A filed 2026-08-12" in after.text

    # Before the 10-Q, the quarter before it is the latest one on file.
    early = datetime.fromisoformat("2026-07-01T00:00:00+00:00")
    before = await source.read(AAPL, as_of=early, params={"now": early})
    assert before is not None and "quarter ended 2026-03-28" in before.text
    assert await source.read(BTC, as_of=NOW, params={"now": NOW}) is None  # not a filer


async def test_live_only_vendor_rows_serve_replay_only_from_their_fetch_on():
    source = PolymarketSource(StubVendorReads())
    assert await source.read(BTC, as_of=NOW - timedelta(hours=3), params={}) is None
    pinned = await source.read(BTC, as_of=NOW, params={})
    assert pinned is not None and pinned.version == 3
    assert "Will the Fed cut rates in September? — Yes 62%" in pinned.text
    live = await source.read(BTC, as_of=None, params={"now": NOW})
    assert live is not None and live.version == 4  # live reads the newest row


# --- replay, end to end ---------------------------------------------------------------


async def test_replay_steps_see_vendor_rows_only_from_their_fetch_on(db: Database):
    await seed_hourly_bars(db)
    repo = VendorReadsRepo(db)
    seen = await repo.add(source="polymarket", symbol=None, payload=POLY_NOW)
    late = await repo.add(source="polymarket", symbol=None, payload=POLY_LATER)
    retail = await repo.add(source="retail_sentiment", symbol="BTC/USD", payload=RETAIL_NOW)
    for row_id, ts in (
        (seen, NOW - timedelta(minutes=90)),
        (late, NOW + timedelta(hours=1)),
        (retail, NOW - timedelta(minutes=90)),
    ):
        await db.execute("UPDATE vendor_reads SET ts = %s WHERE id = %s", (ts, row_id))

    harness = ReplayHarness(db, StubLLM([HOLD, HOLD]), StubMarketData())
    run = await harness.run(
        BTC,
        NOW - timedelta(hours=2),
        NOW,
        label="pit",
        config={"sources": [{"source": "polymarket"}, {"source": "retail_sentiment"}]},
    )
    assert run["status"] == "done"
    steps = await db.fetch_all("SELECT ts, prompt, decision FROM eval_steps ORDER BY ts")
    assert len(steps) == 2
    first, second = steps
    assert first["decision"]["source_versions"] == {}  # both rows were fetched after it
    assert "Polymarket odds" not in first["prompt"]
    versions = second["decision"]["source_versions"]
    assert versions["polymarket"]["version"] == seen
    assert versions["retail_sentiment"]["version"] == retail
    assert "Will the Fed cut rates in September?" in second["prompt"]
    assert "StockTwits: 30+ messages in 24h" in second["prompt"]
    for step in steps:
        assert "FUTURE" not in step["prompt"]


def test_blind_replay_drops_every_vendor_lane():
    kept, dropped = split_sources([{"source": name} for name in VENDOR_SOURCES])
    assert kept == [] and dropped == sorted(VENDOR_SOURCES)
