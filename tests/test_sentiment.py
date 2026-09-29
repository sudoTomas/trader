"""X sentiment via xAI server-side x_search + sentiment wake trigger
(see docs/lessons.md#sentiment-no-citations-is-unavailable).

The xAI /v1/responses endpoint is stubbed with httpx.MockTransport; the store is real.
The load-bearing rule: zero citations = model-prior, not live X → `unavailable`, never
a fabricated number.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from trader.core.symbols import default_registry
from trader.loop.scheduler import SENTIMENT, Mark, SignalScheduler, TriggerConfig
from trader.ops.config import Settings
from trader.research.sentiment import SentimentService, XSentimentClient
from trader.store.db import Database
from trader.store.repos import SentimentRepo
from trader.surfaces.api.app import create_app

ETH = default_registry().get("ETH/USD")
NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)

VERDICT = json.dumps({"score": 0.62, "volume": "elevated", "drivers": ["ETF inflows"]})


def responses_payload(text: str, citations: list[str]) -> dict:
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "citations": citations,
        "usage": {"input_tokens": 100, "output_tokens": 40},
    }


def make_client(payload: dict, captured: list[dict]) -> XSentimentClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    http = httpx.AsyncClient(base_url="https://api.x.ai/v1", transport=httpx.MockTransport(handler))
    return XSentimentClient(http, model="grok-4")


def service(db: Database, client: XSentimentClient, **kwargs) -> SentimentService:
    return SentimentService(db, client, **kwargs)


# --- client request shape ----------------------------------------------------


async def test_request_uses_server_side_x_search_with_date_filters(db: Database):
    captured: list[dict] = []
    client = make_client(responses_payload(VERDICT, ["https://x.com/i/status/1"]), captured)
    await service(db, client).get_x_sentiment(ETH, now=NOW)

    body = captured[0]
    assert body["model"] == "grok-4"
    assert body["store"] is False
    (tool,) = body["tools"]
    assert tool["type"] == "x_search"
    assert tool["to_date"] == "2026-08-12"
    assert tool["from_date"] == "2026-08-11"  # 24h lookback, dates only
    assert "ETH" in json.dumps(body["input"])


# --- the degraded rule -------------------------------------------------------


async def test_zero_citations_is_unavailable_never_a_number(db: Database):
    client = make_client(responses_payload(VERDICT, []), [])  # plausible text, no citations
    result = await service(db, client).get_x_sentiment(ETH, now=NOW)
    assert result["status"] == "unavailable"
    assert "score" not in result or result["score"] is None

    (row,) = await db.fetch_all("SELECT * FROM sentiment_scores")
    assert row["status"] == "unavailable"
    assert row["score"] is None


async def test_live_citations_yield_a_persisted_scored_verdict(db: Database):
    client = make_client(responses_payload(VERDICT, ["https://x.com/i/status/1"]), [])
    result = await service(db, client).get_x_sentiment(ETH, now=NOW)
    assert result["status"] == "ok"
    assert result["score"] == 0.62
    assert result["volume"] == "elevated"

    row = await SentimentRepo(db).latest("ETH/USD")
    assert row is not None and float(row["score"]) == 0.62
    assert row["citations"] == ["https://x.com/i/status/1"]

    # Metered into usage_daily under the sentiment role (M2 discipline).
    (usage,) = await db.fetch_all("SELECT * FROM usage_daily WHERE role = 'sentiment'")
    assert usage["prompt_tokens"] == 100 and usage["completion_tokens"] == 40
    # Tokens (grok-4 $3/$15) + pre-cutover x_search call fee ($5/1k calls).
    token = (100 * 3.00 + 40 * 15.00) / 1_000_000
    assert float(usage["cost_usd"]) == pytest.approx(token + 5.0 / 1000)


async def test_sentiment_meters_x_search_per_post_after_cutover(db: Database):
    after = datetime(2026, 9, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    cites = ["https://x.com/i/status/1", "https://x.com/foo/status/2", "https://x.com/bar"]
    client = make_client(responses_payload(VERDICT, cites), [])
    await service(db, client).get_x_sentiment(ETH, now=after)
    (usage,) = await db.fetch_all("SELECT * FROM usage_daily WHERE role = 'sentiment'")
    token = (100 * 3.00 + 40 * 15.00) / 1_000_000
    tool = 2 * 5.0 / 1000 + 1 * 10.0 / 1000  # 2 posts + 1 profile
    assert float(usage["cost_usd"]) == pytest.approx(token + tool)


async def test_daily_call_cap_short_circuits_without_calling_x(db: Database):
    captured: list[dict] = []
    client = make_client(responses_payload(VERDICT, ["https://x.com/i/status/1"]), captured)
    svc = service(db, client, daily_call_cap=2)
    await svc.get_x_sentiment(ETH, now=NOW)
    await svc.get_x_sentiment(ETH, now=NOW)
    capped = await svc.get_x_sentiment(ETH, now=NOW)

    assert capped["status"] == "unavailable" and "cap" in capped["reason"]
    assert len(captured) == 2  # the third never reached the wire


async def test_unparseable_verdict_is_unavailable(db: Database):
    client = make_client(
        responses_payload("chatty prose, no JSON", ["https://x.com/i/status/1"]), []
    )
    result = await service(db, client).get_x_sentiment(ETH, now=NOW)
    assert result["status"] == "unavailable"


# --- the sentiment wake trigger ----------------------------------------------


def test_sentiment_trigger_is_shadow_gated_off_by_default():
    assert TriggerConfig().sentiment_enabled is False
    assert Settings(auth_disabled=True).sentiment_trigger_enabled is False

    scheduler = SignalScheduler([ETH], TriggerConfig())
    scheduler.mark_dispatched(scheduler.evaluate(NOW, {"ETH/USD": Mark(price=2000)}), NOW)
    later = NOW + timedelta(minutes=1)
    fired = scheduler.evaluate(later, {"ETH/USD": Mark(price=2000, sentiment_score=0.9)})
    scheduler.evaluate(
        later + timedelta(minutes=1), {"ETH/USD": Mark(price=2000, sentiment_score=-0.9)}
    )
    assert SENTIMENT not in [t.type for t in fired]


def test_sentiment_swing_triggers_when_enabled_and_respects_cooldown():
    cfg = TriggerConfig(sentiment_enabled=True, sentiment_swing=0.5, cooldown_sentiment=3600)
    scheduler = SignalScheduler([ETH], cfg)
    t0 = scheduler.evaluate(NOW, {"ETH/USD": Mark(price=2000, sentiment_score=0.1)})
    assert SENTIMENT not in [t.type for t in t0]  # first observation is baseline only
    scheduler.mark_dispatched(t0, NOW)

    later = NOW + timedelta(minutes=20)
    fired = scheduler.evaluate(later, {"ETH/USD": Mark(price=2000, sentiment_score=-0.6)})
    swing = [t for t in fired if t.type == SENTIMENT]
    assert len(swing) == 1 and abs(swing[0].detail["delta"]) >= 0.5
    scheduler.mark_dispatched(fired, later)

    soon = scheduler.evaluate(
        later + timedelta(minutes=10), {"ETH/USD": Mark(price=2000, sentiment_score=0.6)}
    )
    assert SENTIMENT not in [t.type for t in soon]  # cooldown holds


def test_degraded_scores_never_trigger():
    cfg = TriggerConfig(sentiment_enabled=True, sentiment_swing=0.1)
    scheduler = SignalScheduler([ETH], cfg)
    scheduler.evaluate(NOW, {"ETH/USD": Mark(price=2000, sentiment_score=0.8)})
    # A degraded/unavailable poll surfaces as None — the trigger must stay silent.
    fired = scheduler.evaluate(
        NOW + timedelta(minutes=20), {"ETH/USD": Mark(price=2000, sentiment_score=None)}
    )
    assert SENTIMENT not in [t.type for t in fired]


# --- surfaces ----------------------------------------------------------------


def test_sentiment_history_surfaced_on_api(clean_db: None):
    import asyncio

    async def seed() -> None:
        database = Database(min_size=1, max_size=1)
        await database.open()
        try:
            await SentimentRepo(database).insert(
                symbol="ETH/USD",
                status="ok",
                score=0.4,
                volume="normal",
                drivers=["funding"],
                citations=["https://x.com/i/status/9"],
                model="grok-4",
            )
        finally:
            await database.close()

    asyncio.run(seed())
    with TestClient(create_app(Settings(auth_disabled=True))) as tc:
        rows = tc.get("/research/sentiment", params={"symbol": "ETH/USD"}).json()
    assert len(rows) == 1
    assert rows[0]["score"] == 0.4
    assert rows[0]["citations"] == ["https://x.com/i/status/9"]


def test_sentiment_capabilities_are_registered():
    from trader.surfaces.services import build_registry

    tools = {c.tool_name for c in build_registry(None).all()}
    assert {"get_sentiment", "fetch_x_sentiment"} <= tools


# --- runner integration ------------------------------------------------------


class RecordingScheduler:
    """Captures the marks the runner feeds the scheduler; fires nothing."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Mark]] = []

    def evaluate(self, now, marks):
        self.seen.append(marks)
        return []

    def mark_dispatched(self, triggers, now):
        pass


class StubSentimentService:
    def __init__(self) -> None:
        self.polled: list[str] = []

    async def get_x_sentiment(self, symbol, **kwargs):
        self.polled.append(symbol.ticker)
        return {"status": "ok", "symbol": symbol.ticker, "score": 0.7}


async def test_runner_polls_on_cadence_and_feeds_held_marks(db: Database):
    from tests.test_loop import NOW as LOOP_NOW
    from tests.test_loop import build_runner
    from trader.store.repos import PositionsRepo

    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=1.0)
    await SentimentRepo(db).insert(symbol="BTC/USD", status="ok", score=0.7, model="grok-4")

    svc = StubSentimentService()
    scheduler = RecordingScheduler()
    runner = build_runner(db, [], scheduler=scheduler, sentiment=svc)

    await runner.tick(LOOP_NOW)
    assert svc.polled == ["BTC/USD"]  # collected for the shadow record
    assert scheduler.seen[0]["BTC/USD"].sentiment_score == 0.7  # held symbol sees the score

    await runner.tick(LOOP_NOW + timedelta(seconds=60))
    assert svc.polled == ["BTC/USD"]  # within the poll cadence: no second wire call


async def test_unheld_symbols_get_no_sentiment_in_their_mark(db: Database):
    from tests.test_loop import NOW as LOOP_NOW
    from tests.test_loop import build_runner

    await SentimentRepo(db).insert(symbol="BTC/USD", status="ok", score=0.7, model="grok-4")
    scheduler = RecordingScheduler()
    runner = build_runner(db, [], scheduler=scheduler, sentiment=StubSentimentService())

    await runner.tick(LOOP_NOW)  # no position held
    assert scheduler.seen[0]["BTC/USD"].sentiment_score is None
