"""CallsDesk: poll -> parse -> link -> score, and the scorecard. Real Postgres through the
``db`` fixture; Telegram, the LLM and the bars are fakes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from trader.calls.desk import PARSE_BATCH, CallsDesk
from trader.calls.poller import ChannelPoller
from trader.calls.reader import RawPost
from trader.core.types import Bar
from trader.llm.base import Completion, Usage
from trader.ops.config import Settings
from trader.store.repos import CallParsesRepo, CallScoresRepo, ChannelPostsRepo

CHANNEL = -1003097376178
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def settings(**kwargs) -> Settings:
    return Settings(_env_file=None, auth_disabled=True, calls_channel_id=CHANNEL, **kwargs)


def raw(msg_id: int, text: str, *, at: datetime, reply_to: int | None = None) -> RawPost:
    return RawPost(
        msg_id=msg_id, posted_at=at, edited_at=None, reply_to=reply_to, text=text, media=None
    )


def call_reply(coin: str, *, side: str = "long", allocation=None, leverage=None) -> str:
    return json.dumps(
        {
            "kind": "call",
            "call": {
                "coin": coin,
                "side": side,
                "entry": 100.0,
                "tps": [110.0, 120.0, 130.0] if side == "long" else [90.0, 80.0, 70.0],
                "stop": None,
                "no_stop": True,
                "allocation_pct": allocation,
                "leverage": leverage,
            },
            "update": None,
            "flags": [],
        }
    )


UPDATE_REPLY = json.dumps(
    {
        "kind": "update",
        "call": None,
        "update": {"coin": "DOGE", "event": "tp_hit", "tp_level": 1, "new_stop": None},
        "flags": [],
    }
)


OTHER_REPLY = json.dumps({"kind": "other", "call": None, "update": None, "flags": []})


class StubLLM:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    async def complete(self, role, messages, *, schema=None, max_tokens=1024, temperature=0.2):
        return Completion(
            text=self._replies.pop(0),
            usage=Usage(prompt_tokens=1, completion_tokens=1),
            cost_usd=0.0,
            provider="stub",
            model="stub",
            profile_version=1,
        )


class FakeReader:
    def __init__(self, posts: list[RawPost]) -> None:
        self.posts = posts

    async def newer(self, channel_id: int, min_id: int, limit: int = 200) -> list[RawPost]:
        return [p for p in sorted(self.posts, key=lambda p: p.msg_id) if p.msg_id > min_id][:limit]

    async def latest(self, channel_id: int, limit: int) -> list[RawPost]:
        return sorted(self.posts, key=lambda p: -p.msg_id)[:limit]

    async def history(self, channel_id: int) -> AsyncIterator[RawPost]:
        for p in sorted(self.posts, key=lambda p: p.msg_id):
            yield p


class FakeMarket:
    """15m bars from each window's start: open 100, high 111 (TP1), low 99, close 105.
    ``fail`` maps a ticker to an exception to raise, or to [] for no bars."""

    def __init__(self, fail: dict | None = None) -> None:
        self.fail = fail or {}
        self.asked: list[tuple[str, str, datetime, datetime]] = []

    async def get_bars_between(self, symbol, interval, start, end) -> list[Bar]:
        self.asked.append((symbol.ticker, interval, start, end))
        if symbol.ticker in self.fail:
            outcome = self.fail[symbol.ticker]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        first = start + timedelta(minutes=15)
        return [
            Bar(symbol.ticker, interval, first + timedelta(minutes=15 * i), 100, 111, 99, 105, 1)
            for i in range(4)
        ]


def desk_for(db, llm, market, reader=None, **kw) -> CallsDesk:
    s = settings(**kw)
    poller = None
    if reader is not None:
        poller = ChannelPoller(db, reader, channel_id=CHANNEL, recheck=0)
    return CallsDesk(db, s, market, llm, poller)


async def seed_call(
    db,
    msg_id: int,
    coin: str,
    *,
    at: datetime = T0,
    backfilled: bool = True,
    side: str = "long",
    allocation=None,
    leverage=None,
) -> None:
    await ChannelPostsRepo(db).upsert_version(
        raw(msg_id, f"{coin} call", at=at), channel_id=CHANNEL, backfilled=backfilled
    )
    await CallParsesRepo(db).add(
        channel_id=CHANNEL,
        msg_id=msg_id,
        version=1,
        kind="call",
        payload=json.loads(call_reply(coin, side=side, allocation=allocation, leverage=leverage)),
        model=None,
        profile_version=None,
        cost_usd=None,
        error=None,
    )


async def test_poll_runs_parse_link_and_score(db) -> None:
    # The backfill has stored something (an empty post), so the poll stores live posts.
    await ChannelPostsRepo(db).upsert_version(
        raw(1, "", at=T0 - timedelta(days=1)), channel_id=CHANNEL, backfilled=True
    )
    reader = FakeReader(
        [
            raw(1, "", at=T0 - timedelta(days=1)),
            raw(2, "DOGE long", at=T0),
            raw(3, "DOGE TP1 HIT", at=T0 + timedelta(hours=2), reply_to=2),
        ]
    )
    market = FakeMarket()
    desk = desk_for(db, StubLLM([call_reply("DOGE"), UPDATE_REPLY]), market, reader)

    out = await desk.poll()

    assert out["poll"] == {"new": 2, "edited": 0}
    assert out["linked"] == 1 and out["scored"] == 1
    assert (await CallParsesRepo(db).get(CHANNEL, 3))["links_to"] == 2
    scores = await CallScoresRepo(db).all(CHANNEL)
    assert [(s["msg_id"], s["tradeable"]) for s in scores] == [(2, True)]
    assert scores[0]["result"]["fills"][1]["reason"] == "tp1"
    # 15m bars from 15 minutes before the post, for DOGE/USD.
    ticker, interval, start, _ = market.asked[0]
    assert (ticker, interval, start) == ("DOGE/USD", "15m", T0 - timedelta(minutes=15))
    assert start.tzinfo is not None

    calls = await desk.calls()
    assert calls[0]["msg_id"] == 2
    assert calls[0]["score"]["status"] in {"open", "closed"}
    assert [u["msg_id"] for u in calls[0]["updates"]] == [3]
    posts = await desk.posts()
    assert [(p["msg_id"], p["kind"]) for p in posts] == [(3, "update"), (2, "call"), (1, "other")]


async def test_unscorable_call_does_not_break_the_scorecard(db) -> None:
    await seed_call(db, 1, "DOGE")
    await seed_call(db, 2, "PENGU", at=T0 + timedelta(hours=1))
    await seed_call(db, 3, "WLD", at=T0 + timedelta(hours=2))
    market = FakeMarket(fail={"PENGU/USD": [], "WLD/USD": RuntimeError("no such asset")})
    desk = desk_for(db, StubLLM([]), market)

    assert await desk.score_pending() == 3
    card = await desk.scorecard()

    assert card["calls"] == 3
    assert card["unscorable"] == 2
    assert card["tradeable"] == 1
    by_status = {s["msg_id"]: s for s in await CallScoresRepo(db).all(CHANNEL)}
    assert by_status[2]["status"] == "unscorable" and not by_status[2]["tradeable"]
    assert by_status[3]["result"]["reason"]
    # Rows unscorable for want of bars are rescored on the next pass; a closed one is not.
    assert by_status[1]["status"] == "closed"
    assert by_status[2]["result"]["reason"] == "bars unavailable: no bars for PENGU/USD"
    # Right away nothing is fetched again; an hour on, only the fetch that raised is
    # retried. An empty fetch past the grace (an unlisted coin) waits for a backfill.
    market.asked.clear()
    assert await desk.score_pending() == 0
    assert market.asked == []
    await age_scores(db, hours=2)
    await desk.score_pending()
    assert {t for t, *_ in market.asked} == {"WLD/USD"}
    market.asked.clear()
    await desk.process(rescore=True)
    assert {t for t, *_ in market.asked} == {"DOGE/USD", "PENGU/USD", "WLD/USD"}


async def test_a_call_unscorable_for_want_of_bars_is_rescored_when_they_arrive(db) -> None:
    await seed_call(db, 1, "DOGE")
    market = FakeMarket(fail={"DOGE/USD": ConnectionError("alpaca is down")})
    desk = desk_for(db, StubLLM([]), market)
    await desk.score_pending()
    (row,) = await CallScoresRepo(db).all(CHANNEL)
    assert row["status"] == "unscorable"
    assert row["result"]["reason"] == "bars unavailable: ConnectionError"

    market.fail.clear()  # the outage ends
    assert await desk.score_pending() == 0  # retried at most hourly
    await age_scores(db, hours=1, minutes=1)
    assert await desk.score_pending() == 1
    (row,) = await CallScoresRepo(db).all(CHANNEL)
    assert row["status"] == "closed" and row["tradeable"] is True


async def age_scores(db, **delta) -> None:
    """Move every stored score's ``scored_at`` back by ``timedelta(**delta)``."""
    await db.execute("UPDATE call_scores SET scored_at = scored_at - %s", (timedelta(**delta),))


def flat_bars(start: datetime, n: int = 4) -> list[Bar]:
    """15m bars from ``start`` that touch neither a TP at 110 nor an 8% stop: open."""
    return [
        Bar("DOGE/USD", "15m", start + timedelta(minutes=15 * i), 100, 105, 99, 102, 1)
        for i in range(n)
    ]


async def test_an_open_call_is_rescored_at_most_hourly(db) -> None:
    await seed_call(db, 1, "DOGE")
    market = FakeMarket(fail={"DOGE/USD": flat_bars(T0)})
    desk = desk_for(db, StubLLM([]), market)
    assert await desk.score_pending() == 1
    (row,) = await CallScoresRepo(db).all(CHANNEL)
    assert row["status"] == "open"

    market.asked.clear()
    assert await desk.score_pending() == 0
    await age_scores(db, minutes=59)
    assert await desk.score_pending() == 0
    assert market.asked == []
    await age_scores(db, minutes=2)
    assert await desk.score_pending() == 1
    assert len(market.asked) == 1


async def test_no_bars_after_the_post_is_classed_as_no_bars(db) -> None:
    # An old call whose bars all predate it: like an empty fetch, stable until a backfill.
    await seed_call(db, 1, "DOGE")
    market = FakeMarket(fail={"DOGE/USD": flat_bars(T0 - timedelta(minutes=15), n=1)})
    desk = desk_for(db, StubLLM([]), market)
    assert await desk.score_pending() == 1
    (row,) = await CallScoresRepo(db).all(CHANNEL)
    assert row["status"] == "unscorable"
    assert row["result"]["reason"] == "bars unavailable: no bars after post for DOGE/USD"

    await age_scores(db, hours=2)
    market.asked.clear()
    assert await desk.score_pending() == 0
    assert market.asked == []
    market.fail.clear()
    assert (await desk.process(rescore=True))["scored"] == 1
    (row,) = await CallScoresRepo(db).all(CHANNEL)
    assert row["status"] == "closed"


async def test_invalid_levels_are_stored_unscorable_not_retried(db) -> None:
    await ChannelPostsRepo(db).upsert_version(
        raw(1, "DOGE call", at=T0), channel_id=CHANNEL, backfilled=True
    )
    await CallParsesRepo(db).add(
        channel_id=CHANNEL,
        msg_id=1,
        version=1,
        kind="call",
        payload={"kind": "call", "call": {"coin": "DOGE"}, "update": None, "flags": []},
        model=None,
        profile_version=None,
        cost_usd=None,
        error=None,
    )
    market = FakeMarket()
    desk = desk_for(db, StubLLM([]), market)

    assert await desk.score_pending() == 1
    (row,) = await CallScoresRepo(db).all(CHANNEL)
    assert (row["status"], row["tradeable"]) == ("unscorable", False)
    assert row["result"]["reason"] == "invalid levels"
    assert await desk.score_pending() == 0  # stored, so not retried every pass
    card = await desk.scorecard()  # and the scorecard still answers
    assert card["calls"] == 1 and card["unscorable"] == 1


async def test_a_scoring_failure_is_stored_unscorable_not_retried(db) -> None:
    await seed_call(db, 1, "DOGE")
    await ChannelPostsRepo(db).upsert_version(
        raw(2, "DOGE ???", at=T0 + timedelta(minutes=20), reply_to=1),
        channel_id=CHANNEL,
        backfilled=True,
    )
    parses = CallParsesRepo(db)
    await parses.add(
        channel_id=CHANNEL,
        msg_id=2,
        version=1,
        kind="update",
        payload={"kind": "update", "call": None, "update": {"event": "nonsense"}, "flags": []},
        model=None,
        profile_version=None,
        cost_usd=None,
        error=None,
    )
    await parses.set_link(CHANNEL, 2, 1)
    desk = desk_for(db, StubLLM([]), FakeMarket())

    assert await desk.score_pending() == 1
    (row,) = await CallScoresRepo(db).all(CHANNEL)
    assert (row["status"], row["tradeable"]) == ("unscorable", False)
    assert row["result"]["reason"] == "score failed: ValidationError"
    assert await desk.score_pending() == 0


async def test_a_fresh_call_waits_for_its_entry_bar(db) -> None:
    await seed_call(db, 1, "DOGE", at=datetime.now(UTC) - timedelta(minutes=5), backfilled=False)
    desk = desk_for(db, StubLLM([]), FakeMarket(fail={"DOGE/USD": []}))

    assert await desk.score_pending() == 0
    assert await CallScoresRepo(db).all(CHANNEL) == []


async def test_scorecard_gate_needs_28_live_days_and_positive_pnl(db) -> None:
    await seed_call(db, 1, "DOGE")
    await seed_call(db, 2, "ETH", at=T0 + timedelta(hours=1))
    desk = desk_for(db, StubLLM([]), FakeMarket())
    await desk.score_pending()

    card = await desk.scorecard()

    assert card["live_days"] == 0
    assert card["gate_pass"] is False
    assert any("live days" in r for r in card["gate_reasons"])
    assert set(card["by_coin"]) == {"DOGE", "ETH"}


async def test_scorecard_gate_passes_with_backfill_live_weeks_and_tp1(db) -> None:
    await seed_call(db, 1, "DOGE")
    await seed_call(db, 2, "ETH", at=datetime.now(UTC) - timedelta(days=30), backfilled=False)
    for msg_id in (1, 2):
        await CallScoresRepo(db).put(
            CHANNEL,
            msg_id,
            status="closed",
            tradeable=True,
            result={"status": "closed", "net_return": 0.05, "first_touch": "tp1"},
        )
    card = await desk_for(db, StubLLM([]), FakeMarket()).scorecard()

    assert card["live_days"] == 30
    assert card["tp1_before_stop"] == 2 and card["tp1_rate"] == 1.0
    assert card["gate_pass"] is True, card["gate_reasons"]
    assert card["gate_reasons"] == []


async def test_sleeve_pnl_mirrors_allocation_times_leverage(db) -> None:
    await seed_call(db, 1, "DOGE", allocation=0.5, leverage=7)
    await seed_call(db, 2, "SOL", side="short", at=T0 + timedelta(hours=1))
    await CallScoresRepo(db).put(
        CHANNEL,
        1,
        status="closed",
        tradeable=True,
        result={"status": "closed", "net_return": 0.10, "first_touch": "tp1"},
    )
    await CallScoresRepo(db).put(
        CHANNEL,
        2,
        status="closed",
        tradeable=False,
        result={"status": "closed", "net_return": -0.02, "first_touch": "stop"},
    )

    card = await desk_for(db, StubLLM([]), FakeMarket()).scorecard()

    assert card["sleeve_pnl_usd"] == 35.0  # $10,000 x 0.5% x 7 x +10%
    assert card["net_return_mean"] == 0.10
    assert card["untradeable"] == {"calls": 1, "net_return_mean": -0.02}


async def put_score(db, msg_id: int, *, net: float, touch: str, status: str = "closed") -> None:
    await CallScoresRepo(db).put(
        CHANNEL,
        msg_id,
        status=status,
        tradeable=True,
        result={"status": status, "net_return": net, "first_touch": touch},
    )


async def test_tp1_rate_counts_only_resolved_calls(db) -> None:
    await seed_call(db, 1, "DOGE")
    await seed_call(db, 2, "ETH", at=T0 + timedelta(hours=1))
    await put_score(db, 1, net=0.05, touch="tp1")
    await put_score(db, 2, net=0.0, touch="neither", status="open")  # not a miss yet

    card = await desk_for(db, StubLLM([]), FakeMarket()).scorecard()

    assert card["tradeable"] == 2 and card["tp1_before_stop"] == 1
    assert card["tp1_rate"] == 1.0


async def gate_card(db, scores: list[tuple[float, str]]) -> dict:
    """Backfilled calls plus one live call 30 days old, with the given (net, first_touch)."""
    await seed_call(db, 1, "DOGE")
    await seed_call(db, 2, "ETH", at=T0 + timedelta(hours=1))
    await seed_call(db, 3, "SOL", at=datetime.now(UTC) - timedelta(days=30), backfilled=False)
    for msg_id, (net, touch) in enumerate(scores, start=1):
        await put_score(db, msg_id, net=net, touch=touch)
    return await desk_for(db, StubLLM([]), FakeMarket()).scorecard()


async def test_the_gate_fails_on_pnl_alone(db) -> None:
    card = await gate_card(db, [(0.05, "tp1"), (-0.10, "tp1"), (0.0, "stop")])

    assert card["live_days"] >= 28 and card["tp1_rate"] >= 0.5
    assert card["sleeve_pnl_usd"] < 0
    assert card["gate_pass"] is False
    assert card["gate_reasons"] == ["sleeve net P&L $-5.00 is not positive"]


async def test_the_gate_fails_on_tp1_rate_alone(db) -> None:
    card = await gate_card(db, [(0.30, "tp1"), (-0.02, "stop"), (-0.02, "stop")])

    assert card["live_days"] >= 28 and card["sleeve_pnl_usd"] > 0
    assert card["tp1_rate"] == 1 / 3
    assert card["gate_pass"] is False
    assert card["gate_reasons"] == ["1 of 3 resolved tradeable calls reached TP1 before the stop"]


async def test_process_parses_past_one_batch(db) -> None:
    n = PARSE_BATCH * 2 + 5
    posts = ChannelPostsRepo(db)
    for msg_id in range(1, n + 1):
        await posts.upsert_version(
            raw(msg_id, f"chatter {msg_id}", at=T0), channel_id=CHANNEL, backfilled=True
        )
    desk = desk_for(db, StubLLM([OTHER_REPLY] * n), FakeMarket())

    out = await desk.process()

    assert out["parsed"] == {"other": n}
    assert await posts.unparsed(CHANNEL, 10) == []
