"""Telegram channel-calls poller: new posts, edit versions, backfill, failure recovery.
Real Postgres through the ``db`` fixture; Telegram is a fake reader."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from trader.calls.poller import ChannelPoller
from trader.calls.reader import RawPost
from trader.store.repos import CallParsesRepo, ChannelPostsRepo

CHANNEL = -1003097376178
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def post(msg_id: int, text: str | None = None) -> RawPost:
    return RawPost(
        msg_id=msg_id,
        posted_at=T0 + timedelta(minutes=msg_id),
        edited_at=None,
        reply_to=None,
        text=text if text is not None else f"BTC long #{msg_id} TP 72000 SL 68000",
        media=None,
    )


class FakeReader:
    """A ChannelReader over a list of posts; ``fail_newer`` raises on that many calls."""

    def __init__(self, posts: list[RawPost], *, fail_newer: int = 0) -> None:
        self.posts = posts
        self.fail_newer = fail_newer

    def _sorted(self) -> list[RawPost]:
        return sorted(self.posts, key=lambda p: p.msg_id)

    async def newer(self, channel_id: int, min_id: int, limit: int = 200) -> list[RawPost]:
        assert channel_id == CHANNEL
        if self.fail_newer:
            self.fail_newer -= 1
            raise ConnectionError("telegram is down")
        return [p for p in self._sorted() if p.msg_id > min_id][:limit]

    async def latest(self, channel_id: int, limit: int) -> list[RawPost]:
        assert channel_id == CHANNEL
        return list(reversed(self._sorted()))[:limit]

    async def history(self, channel_id: int) -> AsyncIterator[RawPost]:
        assert channel_id == CHANNEL
        for p in self._sorted():
            yield p


async def rows(db) -> list[dict]:
    return await db.fetch_all(
        "SELECT msg_id, version, text, backfilled FROM channel_posts ORDER BY msg_id, version"
    )


async def test_poll_stores_new_posts_after_max_msg_id(db) -> None:
    await ChannelPostsRepo(db).upsert_version(post(1), channel_id=CHANNEL, backfilled=True)
    reader = FakeReader([post(1), post(2), post(3)])
    poller = ChannelPoller(db, reader, channel_id=CHANNEL, recheck=0)

    assert await poller.poll_once() == {"new": 2, "edited": 0}

    stored = await rows(db)
    assert [(r["msg_id"], r["backfilled"]) for r in stored] == [(1, True), (2, False), (3, False)]
    assert await poller.poll_once() == {"new": 0, "edited": 0}


async def test_poll_pages_through_more_than_one_batch(db) -> None:
    await ChannelPostsRepo(db).upsert_version(post(1), channel_id=CHANNEL, backfilled=True)
    reader = FakeReader([post(i) for i in range(1, 451)])
    poller = ChannelPoller(db, reader, channel_id=CHANNEL, recheck=0)

    assert await poller.poll_once() == {"new": 449, "edited": 0}


async def test_poll_waits_for_backfill_when_the_store_is_empty(db) -> None:
    # Old posts read before a backfill would be stored as live and skew the gate.
    poller = ChannelPoller(db, FakeReader([post(1), post(2)]), channel_id=CHANNEL, recheck=10)

    assert await poller.poll_once() == {"new": 0, "edited": 0}
    assert await rows(db) == []


async def test_edit_adds_a_version_and_keeps_the_first_parse(db) -> None:
    reader = FakeReader([post(1), post(2)])
    poller = ChannelPoller(db, reader, channel_id=CHANNEL, recheck=10)
    await poller.backfill()
    reader.posts.append(post(3))
    assert await poller.poll_once() == {"new": 1, "edited": 0}
    await CallParsesRepo(db).add(
        channel_id=CHANNEL,
        msg_id=3,
        version=1,
        kind="call",
        payload={"coin": "BTC", "tp": [72000]},
        model="m",
        profile_version=None,
        cost_usd=None,
        error=None,
    )
    parses_before = await db.fetch_all("SELECT * FROM call_parses")

    edited = replace(post(3), text=post(3).text + " -- TP1 hit", edited_at=T0 + timedelta(hours=2))
    reader.posts = [post(1), post(2), edited]
    assert await poller.poll_once() == {"new": 0, "edited": 1}

    latest = await ChannelPostsRepo(db).latest(CHANNEL, 3)
    assert latest is not None and latest["version"] == 2 and latest["text"] == edited.text
    assert latest["backfilled"] is False
    assert await db.fetch_all("SELECT * FROM call_parses") == parses_before
    assert await poller.poll_once() == {"new": 0, "edited": 0}


async def test_edit_of_a_backfilled_post_stays_backfilled(db) -> None:
    reader = FakeReader([post(1)])
    poller = ChannelPoller(db, reader, channel_id=CHANNEL, recheck=10)
    await poller.backfill()
    reader.posts = [replace(post(1), text="BTC closed +12%", edited_at=T0)]

    assert await poller.poll_once() == {"new": 0, "edited": 1}
    assert [r["backfilled"] for r in await rows(db)] == [True, True]


async def test_backfill_marks_posts_backfilled_and_is_idempotent(db) -> None:
    poller = ChannelPoller(
        db, FakeReader([post(i) for i in range(1, 6)]), channel_id=CHANNEL, recheck=10
    )

    assert await poller.backfill() == {"stored": 5}
    first = await rows(db)
    assert len(first) == 5 and all(r["backfilled"] for r in first)

    assert await poller.backfill() == {"stored": 0}
    assert await rows(db) == first


async def test_poll_is_idempotent_after_a_failure(db) -> None:
    posts = [post(i) for i in range(1, 5)]
    await ChannelPostsRepo(db).upsert_version(posts[0], channel_id=CHANNEL, backfilled=True)
    stopping = asyncio.Event()
    passes: list[int] = []

    async def on_new() -> None:
        passes.append(1)
        stopping.set()

    poller = ChannelPoller(
        db, FakeReader(posts, fail_newer=1), channel_id=CHANNEL, recheck=10, on_new=on_new
    )
    await asyncio.wait_for(poller.run_forever(0.01, stopping), timeout=5)

    assert passes == [1]
    failed_then_ok = await rows(db)

    # The same posts through one clean poll on a fresh store give the same rows.
    await db.execute("TRUNCATE channel_posts CASCADE")
    await ChannelPostsRepo(db).upsert_version(posts[0], channel_id=CHANNEL, backfilled=True)
    await ChannelPoller(db, FakeReader(posts), channel_id=CHANNEL, recheck=10).poll_once()
    assert await rows(db) == failed_then_ok


async def test_run_forever_skips_on_new_when_nothing_was_stored(db) -> None:
    await ChannelPostsRepo(db).upsert_version(post(1), channel_id=CHANNEL, backfilled=True)
    called: list[int] = []

    async def on_new() -> None:
        called.append(1)

    stopping = asyncio.Event()
    poller = ChannelPoller(db, FakeReader([post(1)]), channel_id=CHANNEL, recheck=10, on_new=on_new)
    task = asyncio.create_task(poller.run_forever(0.01, stopping))
    await asyncio.sleep(0.1)
    stopping.set()
    await asyncio.wait_for(task, timeout=5)
    assert called == []


# --- the lifespan task --------------------------------------------------------------------


class LifespanReader(FakeReader):
    def __init__(self, *, fail_connect: bool = False) -> None:
        super().__init__([])
        self.fail_connect = fail_connect
        self.entered = self.exited = False

    async def __aenter__(self) -> LifespanReader:
        if self.fail_connect:
            raise RuntimeError("the stored Telegram session is not authorized")
        self.entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True


def boot_settings(**kwargs):
    from trader.ops.config import Settings

    return Settings(_env_file=None, auth_disabled=True, calls_channel_id=CHANNEL, **kwargs)


def lifespan_spies(monkeypatch, reader):
    import trader.surfaces.api.app as app_module

    runs: list[dict] = []

    class SpyPoller(ChannelPoller):
        async def run_forever(self, interval_seconds: float, stopping: asyncio.Event) -> None:
            runs.append({"interval": interval_seconds, "task": asyncio.current_task().get_name()})
            try:
                await stopping.wait()
            finally:  # shutdown sets ``stopping`` and cancels, as for settlement
                runs[-1]["ended"] = stopping.is_set()

    async def fake_reader_from_store(db, settings):
        return reader

    monkeypatch.setattr(app_module, "reader_from_store", fake_reader_from_store)
    monkeypatch.setattr(app_module, "ChannelPoller", SpyPoller)
    return app_module, runs


def test_lifespan_runs_the_poller_while_connected(clean_db: None, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    reader = LifespanReader()
    app_module, runs = lifespan_spies(monkeypatch, reader)
    s = boot_settings(calls_enabled=True, calls_poll_seconds=7.0)
    with TestClient(app_module.create_app(s)) as tc:
        assert tc.get("/health").status_code == 200
        assert reader.entered and not reader.exited
        assert runs and runs[0]["interval"] == 7.0
        assert runs[0]["task"] == "trader-calls-poller"
    assert runs[0].get("ended") is True
    assert reader.exited


def test_lifespan_skips_the_poller_when_disabled(clean_db: None, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    reader = LifespanReader()
    app_module, runs = lifespan_spies(monkeypatch, reader)
    with TestClient(app_module.create_app(boot_settings(calls_enabled=False))) as tc:
        assert tc.get("/health").status_code == 200
    assert runs == [] and not reader.entered


def test_lifespan_boots_without_a_session_or_a_connection(clean_db: None, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    for reader in (None, LifespanReader(fail_connect=True)):
        app_module, runs = lifespan_spies(monkeypatch, reader)
        with TestClient(app_module.create_app(boot_settings(calls_enabled=True))) as tc:
            assert tc.get("/health").status_code == 200
        assert runs == []
