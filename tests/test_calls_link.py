"""Linking ``update`` parses to their calls. Real Postgres through the ``db`` fixture."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trader.calls.link import link_updates
from trader.calls.reader import RawPost
from trader.store.repos import CallParsesRepo, ChannelPostsRepo

CHANNEL = -1003097376178
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


async def store(
    db,
    msg_id: int,
    kind: str,
    *,
    coin: str | None = None,
    reply_to: int | None = None,
    at: datetime | None = None,
) -> None:
    """A post (first version) and its parse; ``coin`` goes in the call or the update."""
    post = RawPost(
        msg_id=msg_id,
        posted_at=at or T0 + timedelta(minutes=msg_id),
        edited_at=None,
        reply_to=reply_to,
        text=f"post {msg_id}",
        media=None,
    )
    assert await ChannelPostsRepo(db).upsert_version(post, channel_id=CHANNEL, backfilled=False)
    call = update = None
    if kind == "call":
        call = {"coin": coin, "side": "long", "entry": 1.0, "tps": [1.1], "stop": 0.9}
    elif kind == "update":
        update = {"coin": coin, "event": "tp_hit", "tp_level": 1, "new_stop": None}
    await CallParsesRepo(db).add(
        channel_id=CHANNEL,
        msg_id=msg_id,
        version=1,
        kind=kind,
        payload={"kind": kind, "call": call, "update": update, "flags": []},
        model="m",
        profile_version=None,
        cost_usd=None,
        error=None,
    )


async def links_to(db, msg_id: int) -> int | None:
    row = await CallParsesRepo(db).get(CHANNEL, msg_id)
    assert row is not None
    return row["links_to"]


async def test_direct_reply_links(db) -> None:
    await store(db, 1, "call", coin="DOGE")
    await store(db, 2, "update", coin=None, reply_to=1)
    assert await link_updates(db, CHANNEL) == 1
    assert await links_to(db, 2) == 1
    assert await link_updates(db, CHANNEL) == 0


async def test_reply_to_an_update_walks_to_the_call(db) -> None:
    await store(db, 1, "call", coin="DOGE")
    await store(db, 2, "call", coin="DOGE")  # newer same-coin call: the chain must win
    await store(db, 3, "update", coin="DOGE", reply_to=1)
    await store(db, 4, "update", coin="DOGE", reply_to=3)
    assert await link_updates(db, CHANNEL) == 2
    assert await links_to(db, 3) == 1
    assert await links_to(db, 4) == 1


async def test_reply_to_commentary_falls_back_to_same_coin_recent_call(db) -> None:
    await store(db, 1, "call", coin="DOGE")
    await store(db, 2, "call", coin="SOL")
    await store(db, 3, "call", coin="DOGE")
    await store(db, 4, "other")
    await store(db, 5, "update", coin="doge", reply_to=4)
    await store(db, 6, "call", coin="DOGE")  # after the update: not a candidate
    assert await link_updates(db, CHANNEL) == 1
    assert await links_to(db, 5) == 3


async def test_no_match_stays_unlinked(db) -> None:
    await store(db, 1, "call", coin="SOL")
    await store(db, 2, "update", coin="DOGE")
    await store(db, 3, "call", coin="ADA", at=T0 - timedelta(days=31))
    await store(db, 4, "update", coin="ADA")
    await store(db, 5, "update", coin=None)
    assert await link_updates(db, CHANNEL) == 0
    for msg_id in (2, 4, 5):
        assert await links_to(db, msg_id) is None


async def test_a_reply_cycle_or_missing_parent_falls_back(db) -> None:
    await store(db, 1, "call", coin="DOGE")
    await store(db, 2, "update", coin="DOGE", reply_to=3)
    await store(db, 3, "update", coin="DOGE", reply_to=2)
    await store(db, 4, "update", coin="DOGE", reply_to=999)
    assert await link_updates(db, CHANNEL) == 3
    for msg_id in (2, 3, 4):
        assert await links_to(db, msg_id) == 1
