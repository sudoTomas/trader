"""Telegram channel calls store (Phase 1 scorecard): versioned posts, one parse per post,
upserted scores. Real Postgres through the ``db`` fixture; no Telegram."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest

from trader.calls.reader import RawPost
from trader.store.repos import CallParsesRepo, CallScoresRepo, ChannelPostsRepo

CHANNEL = -1003097376178
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def post(msg_id: int, text: str = "BTC long 70000 TP 72000 SL 68000") -> RawPost:
    return RawPost(
        msg_id=msg_id, posted_at=T0, edited_at=None, reply_to=None, text=text, media=None
    )


async def parse(repo: CallParsesRepo, msg_id: int) -> int:
    return await repo.add(
        channel_id=CHANNEL,
        msg_id=msg_id,
        version=1,
        kind="call",
        payload={"coin": "BTC"},
        model="m",
        profile_version=None,
        cost_usd=0.001,
        error=None,
    )


async def test_upsert_version_inserts_once_and_versions_edits(db) -> None:
    posts = ChannelPostsRepo(db)
    original = post(10)
    assert await posts.upsert_version(original, channel_id=CHANNEL, backfilled=False) is True
    assert await posts.upsert_version(original, channel_id=CHANNEL, backfilled=False) is False
    rows = await db.fetch_all("SELECT * FROM channel_posts")
    assert len(rows) == 1 and rows[0]["version"] == 1

    edited = replace(original, text="BTC long 70000 TP 72000 SL 68000 -- TP1 hit")
    assert await posts.upsert_version(edited, channel_id=CHANNEL, backfilled=False) is True
    latest = await posts.latest(CHANNEL, 10)
    assert latest is not None and latest["version"] == 2
    first = await posts.first(CHANNEL, 10)
    assert first is not None and first["text"] == original.text
    assert await posts.max_msg_id(CHANNEL) == 10


async def test_unparsed_lists_first_versions_without_a_parse(db) -> None:
    posts = ChannelPostsRepo(db)
    for msg_id in (1, 2):
        await posts.upsert_version(post(msg_id), channel_id=CHANNEL, backfilled=True)
    await parse(CallParsesRepo(db), 1)
    rows = await posts.unparsed(CHANNEL)
    assert [r["msg_id"] for r in rows] == [2]


async def test_one_parse_per_post(db) -> None:
    await ChannelPostsRepo(db).upsert_version(post(5), channel_id=CHANNEL, backfilled=False)
    parses = CallParsesRepo(db)
    await parse(parses, 5)
    with pytest.raises(psycopg.errors.UniqueViolation):
        await parse(parses, 5)


async def test_scores_upsert(db) -> None:
    await ChannelPostsRepo(db).upsert_version(post(7), channel_id=CHANNEL, backfilled=False)
    await parse(CallParsesRepo(db), 7)
    scores = CallScoresRepo(db)
    await scores.put(CHANNEL, 7, status="open", tradeable=True, result={"pnl": 0})
    await scores.put(CHANNEL, 7, status="closed", tradeable=True, result={"pnl": 12.5})
    rows = await scores.all(CHANNEL)
    assert len(rows) == 1 and rows[0]["status"] == "closed"
    assert rows[0]["result"] == {"pnl": 12.5}


async def test_upsert_version_concurrent_duplicates_insert_once(db) -> None:
    posts = ChannelPostsRepo(db)
    results = await asyncio.gather(
        *(posts.upsert_version(post(3), channel_id=CHANNEL, backfilled=False) for _ in range(4))
    )
    assert sorted(results) == [False, False, False, True]
    assert len(await db.fetch_all("SELECT * FROM channel_posts")) == 1
