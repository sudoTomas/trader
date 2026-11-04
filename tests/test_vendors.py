"""Vendor producers (#142): SEC EDGAR, Polymarket, StockTwits/Reddit — each writes
versioned ``vendor_reads`` rows its Source reads. Fixture-based: every HTTP call goes to
``httpx.MockTransport`` (tests/vendor_fixtures.py), never the internet.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client
from typer.testing import CliRunner

from tests.source_fixtures import BTC, SPY
from tests.test_loop import HOLD, NOW, build_runner
from tests.test_research_compiler import CapturingLLM, reply
from tests.vendor_fixtures import (
    AAPL,
    EDGAR_FACTS,
    EDGAR_SUBMISSIONS,
    EDGAR_TICKERS,
    GAMMA_SEARCH,
    POLY_NOW,
    SEC_PAYLOAD,
    reddit_atom,
    stocktwits_stream,
    transport,
)
from trader.loop.sources import bot_one_subscriptions
from trader.ops.config import Settings
from trader.research.compiler import SYSTEM_PROMPT, MacroViewCompiler
from trader.research.edgar import EdgarProducer
from trader.research.notes import ResearchDesk
from trader.research.polymarket import PolymarketProducer
from trader.research.retail import RetailSentimentProducer
from trader.research.vendors import VendorDesk, build_vendor_desk
from trader.store.db import Database
from trader.store.repos import VendorReadsRepo
from trader.surfaces.api.app import create_app
from trader.surfaces.mcp.server import build_mcp
from trader.surfaces.services import build_registry

UA = "Salamander research test@example.org"
EDGAR_ROUTES = {
    "www.sec.gov/files/company_tickers.json": EDGAR_TICKERS,
    "data.sec.gov/submissions/CIK0000320193.json": EDGAR_SUBMISSIONS,
    "data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json": EDGAR_FACTS,
}


def client(routes: dict[str, Any], seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport(routes, seen))


# --- SEC EDGAR ------------------------------------------------------------------------


async def test_edgar_records_recent_filings_and_as_filed_facts_dated_by_acceptance():
    seen: list[httpx.Request] = []
    producer = EdgarProducer(client(EDGAR_ROUTES, seen), user_agent=UA)
    payload = await producer.fetch(AAPL, now=NOW)
    assert payload is not None
    assert all(r.headers["user-agent"] == UA for r in seen)  # SEC refuses anonymous callers

    # Filings: 8-K / 10-Q / 10-K only (the Form 4 is not one), acceptance read as New
    # York time whatever its "Z" says, so a 13:00 acceptance is 17:00 UTC.
    filings = {f["accession"]: f for f in payload["filings"]}
    assert [f["form"] for f in payload["filings"]] == ["10-Q/A", "8-K", "10-Q"]
    for want in SEC_PAYLOAD["filings"]:
        got = filings[want["accession"]]
        assert {k: got[k] for k in want} == want

    facts = payload["facts"]
    revenue = [f for f in facts if f["item"] == "Revenue"]
    # First tag wins (never summed with "Revenues"); old periods are not kept; each
    # vintage is dated by its own filing's acceptance.
    assert sorted(f["val"] for f in revenue) == [90e9, 94e9, 424242e6]
    restated = next(f for f in revenue if f["form"] == "10-Q/A")
    assert restated["available_at"] == "2026-08-12T17:00:00+00:00"
    # A filing not in the recent list is dated by the end of its filing day (New York).
    older = next(f for f in revenue if f["end"] == "2026-03-28")
    assert older["available_at"] == "2026-05-02T04:00:00+00:00"
    assert {f["item"] for f in facts} == {"Revenue", "Total Assets", "Diluted EPS"}
    eps = next(f for f in facts if f["item"] == "Diluted EPS")
    assert eps["unit"] == "USD/shares" and eps["val"] == 1.57


async def test_edgar_skips_what_is_not_a_us_filer():
    producer = EdgarProducer(client(EDGAR_ROUTES), user_agent=UA)
    assert not producer.serves(BTC)
    assert await producer.fetch(SPY, now=NOW) is None  # an ETF trust absent from the map


def test_edgar_needs_a_user_agent_with_a_contact():
    with pytest.raises(ValueError, match="contact"):
        EdgarProducer(client({}), user_agent="Salamander")


def test_without_a_user_agent_the_edgar_producer_is_absent(monkeypatch: pytest.MonkeyPatch):
    keyless = build_vendor_desk(None, Settings(_env_file=None, auth_disabled=True))  # type: ignore[arg-type]
    assert set(keyless.producers) == {"polymarket", "retail_sentiment"}
    keyed = build_vendor_desk(
        None,  # type: ignore[arg-type]
        Settings(_env_file=None, auth_disabled=True, sec_edgar_user_agent=UA),
    )
    assert set(keyed.producers) == {"polymarket", "retail_sentiment", "sec_filings"}


# --- Polymarket -----------------------------------------------------------------------


async def test_polymarket_keeps_the_most_traded_open_markets_per_topic():
    seen: list[httpx.Request] = []
    producer = PolymarketProducer(
        client({"gamma-api.polymarket.com/public-search": GAMMA_SEARCH}, seen),
        topics=["Fed decision"],
    )
    payload = await producer.fetch(None, now=NOW)
    assert payload is not None
    assert seen[0].url.params["q"] == "Fed decision"
    (topic,) = payload["topics"]
    assert [m["question"] for m in topic["markets"]] == [
        "Will the Fed cut rates in September?",
        "Will the Fed hike in September?",
    ]  # the closed and the past-dated markets are not forward-looking
    assert topic["markets"][0] == POLY_NOW["topics"][0]["markets"][0]


async def test_polymarket_with_every_topic_failing_records_nothing():
    producer = PolymarketProducer(client({}), topics=["CPI", "recession"])
    assert await producer.fetch(None, now=NOW) is None


# --- StockTwits / Reddit --------------------------------------------------------------


def retail_routes(*, stocktwits: Any = None, reddit: Any = None) -> dict[str, Any]:
    return {
        "api.stocktwits.com/api/2/streams/symbol/": stocktwits or stocktwits_stream(),
        "www.reddit.com/r/": reddit or reddit_atom(2, 30, 24 * 9),
    }


async def test_retail_counts_messages_in_the_window_and_their_labels():
    seen: list[httpx.Request] = []
    producer = RetailSentimentProducer(client(retail_routes(), seen))
    payload = await producer.fetch(BTC, now=NOW)
    assert payload is not None
    # Five in the last day (3 bullish, 1 bearish); the 2-day-old and the later one drop.
    assert payload["stocktwits"] == {
        "status": "ok",
        "messages": 5,
        "bullish": 3,
        "bearish": 1,
        "window_hours": 24,
        "capped": False,
    }
    # Two posts inside the week; one nine days old drops.
    assert payload["reddit"] == {
        "status": "ok",
        "posts": 2,
        "window_days": 7,
        "subreddits": ["CryptoCurrency"],
    }
    assert any(r.url.path.endswith("/BTC.X.json") for r in seen)  # crypto spelling
    assert all("salamander" in r.headers["user-agent"].lower() for r in seen)


async def test_one_feed_down_is_recorded_as_unavailable_both_down_records_nothing():
    down = httpx.Response(403)
    producer = RetailSentimentProducer(client(retail_routes(stocktwits=down)))
    payload = await producer.fetch(SPY, now=NOW)
    assert payload is not None
    assert payload["stocktwits"]["status"] == "unavailable"
    assert payload["reddit"]["subreddits"] == ["wallstreetbets", "stocks", "investing"]
    assert payload["reddit"]["posts"] == 6  # two per subreddit

    both = RetailSentimentProducer(client(retail_routes(stocktwits=down, reddit=down)))
    assert await both.fetch(SPY, now=NOW) is None


# --- the desk: rows, cadence, default off ---------------------------------------------


class FakeProducer:
    def __init__(self, name: str, *, market_wide: bool = False, fail: bool = False) -> None:
        self.name = name
        self.market_wide = market_wide
        self.poll_seconds = 3600.0
        self.fail = fail
        self.calls: list[str | None] = []

    def serves(self, symbol) -> bool:
        return symbol.asset_class == "crypto" or self.market_wide

    async def fetch(self, symbol, *, now):
        self.calls.append(symbol.ticker if symbol is not None else None)
        if self.fail:
            raise httpx.ConnectError("down")
        return {"n": len(self.calls)}


async def test_collect_writes_a_versioned_row_and_says_why_when_it_cannot(db: Database):
    retail = FakeProducer("retail_sentiment")
    desk = VendorDesk(db, [retail, FakeProducer("polymarket", market_wide=True)])
    out = await desk.collect("retail_sentiment", BTC, now=NOW)
    assert out["status"] == "ok" and out["payload"] == {"n": 1}
    row = await VendorReadsRepo(db).latest("retail_sentiment", "BTC/USD")
    assert row is not None and row["id"] == out["id"]

    poly = await desk.collect("polymarket", BTC, now=NOW)  # market-wide: no symbol on the row
    assert (await VendorReadsRepo(db).latest("polymarket", None))["id"] == poly["id"]

    assert (await desk.collect("sec_filings", AAPL, now=NOW))["status"] == "unavailable"
    assert (await desk.collect("retail_sentiment", SPY, now=NOW))["status"] == "unavailable"
    failing = VendorDesk(db, [FakeProducer("retail_sentiment", fail=True)])
    assert (await failing.collect("retail_sentiment", BTC, now=NOW))["status"] == "unavailable"


async def test_only_subscribed_sources_are_collected_once_per_cadence(db: Database):
    retail, poly = FakeProducer("retail_sentiment"), FakeProducer("polymarket", market_wide=True)
    desk = VendorDesk(db, [retail, poly])

    # Bot #1's Sources name no vendor: nothing is fetched (each one is opt-in per Bot).
    assert await desk.collect_due(bot_one_subscriptions(briefs=True), [BTC], now=NOW) == []
    assert retail.calls == poly.calls == []

    subs = [{"source": "retail_sentiment", "params": {}}, {"source": "polymarket"}]
    await desk.collect_due(subs, [BTC, SPY], now=NOW)
    assert retail.calls == ["BTC/USD"] and poly.calls == [None]  # SPY is not served
    await desk.collect_due(subs, [BTC], now=NOW + timedelta(minutes=30))
    assert retail.calls == ["BTC/USD"]  # inside the cadence
    await desk.collect_due(subs, [BTC], now=NOW + timedelta(hours=1))
    assert retail.calls == ["BTC/USD", "BTC/USD"]

    # A fresh desk (a restart) seeds its cadence from the store, not from zero.
    await db.execute("UPDATE vendor_reads SET ts = %s", (NOW + timedelta(hours=1),))
    again = FakeProducer("retail_sentiment")
    await VendorDesk(db, [again]).collect_due(subs, [BTC], now=NOW + timedelta(hours=1, minutes=5))
    assert again.calls == []


async def test_a_bot_subscribed_to_a_vendor_schedules_it_on_every_tick(db: Database):
    retail = FakeProducer("retail_sentiment")
    desk = VendorDesk(db, [retail])
    runner = build_runner(db, [HOLD], vendors=desk)
    runner.sources = [{"source": "retail_sentiment", "params": {}}]
    await runner.tick(NOW)
    await desk.idle()
    assert retail.calls == ["BTC/USD"]


# --- Polymarket into the Macro View compiler ------------------------------------------


async def test_polymarket_odds_are_a_citable_macro_view_input_behind_their_gate(db: Database):
    await ResearchDesk(db).submit(title="Rates", body="Cuts are coming.", asset_classes=["crypto"])
    poly = await VendorReadsRepo(db).add(source="polymarket", symbol=None, payload=POLY_NOW)

    dark = CapturingLLM([reply("", {"crypto": "Cuts priced [note:1]."})])
    await MacroViewCompiler(db, dark).compile_now()
    assert "poly:" not in dark.calls[0][1][1]["content"]
    assert dark.calls[0][1][0]["content"] == SYSTEM_PROMPT  # the dark gate changes nothing

    llm = CapturingLLM([reply(f"Markets price a cut [poly:{poly}].", {"crypto": "x [poly:9]"})])
    view = await MacroViewCompiler(db, llm, polymarket=True).compile_now()
    prompt = llm.calls[0][1][1]["content"]
    assert f"--- poly:{poly} | Polymarket odds" in prompt
    assert "Will the Fed cut rates in September? — Yes 62%" in prompt
    assert "[poly:<id>]" in llm.calls[0][1][0]["content"]
    assert view is not None
    assert view["global_thesis"] == f"Markets price a cut [poly:{poly}]."
    assert view["sections"] == {}  # a ghost Polymarket citation drops its section whole


# --- three doors ----------------------------------------------------------------------


def test_vendor_reads_answer_alike_on_every_door(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def seed_and_read() -> tuple[list, list]:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            await VendorReadsRepo(db).add(source="polymarket", symbol=None, payload=POLY_NOW)
            registry = build_registry(db)
            direct = await registry.get("vendor_reads").handler(source="polymarket")
            async with Client(build_mcp(registry)) as mcp_client:
                call = await mcp_client.call_tool("get_vendor_reads", {"source": "polymarket"})
            return direct, call.structured_content["result"]
        finally:
            await db.close()

    direct, via_mcp = asyncio.run(seed_and_read())
    settings = Settings(auth_disabled=True)
    with TestClient(create_app(settings)) as tc:
        via_api = tc.get("/research/vendors", params={"source": "polymarket"}).json()
        collect = tc.post("/research/vendors/collect", params={"source": "sec_filings"}).json()

    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(cli.app, ["vendors", "history", "--source", "polymarket"])
    assert result.exit_code == 0, result.output
    via_cli = json.loads(result.output)

    assert len(direct) == 1 and direct[0]["payload"] == POLY_NOW
    assert direct == via_mcp == via_api == via_cli
    # No User-Agent in this build: EDGAR is simply absent, and says so.
    assert collect["status"] == "unavailable" and "USER_AGENT" in collect["reason"]
