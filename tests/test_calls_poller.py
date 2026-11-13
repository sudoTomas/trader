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

    def __init__(
        self,
        posts: list[RawPost],
        *,
        fail_newer: int = 0,
        connected: bool = True,
        fail_connect: int = 0,
    ) -> None:
        self.posts = posts
        self.fail_newer = fail_newer
        self.connected = connected
        self.fail_connect = fail_connect  # connect() raises this many times (-1: always)
        self.connects = 0

    async def connect(self) -> None:
        self.connects += 1
        if self.fail_connect:
            self.fail_connect -= 1
            raise ConnectionError("telegram is unreachable")
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False

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


async def test_run_forever_runs_on_idle_on_a_pass_that_stored_nothing(db) -> None:
    await ChannelPostsRepo(db).upsert_version(post(1), channel_id=CHANNEL, backfilled=True)
    reader = FakeReader([post(1), post(2)])
    calls: list[str] = []
    stopping = asyncio.Event()

    async def on_new() -> None:
        calls.append("new")

    async def on_idle() -> None:
        calls.append("idle")
        stopping.set()

    poller = ChannelPoller(
        db, reader, channel_id=CHANNEL, recheck=10, on_new=on_new, on_idle=on_idle
    )
    await asyncio.wait_for(poller.run_forever(0.01, stopping), timeout=5)
    assert calls == ["new", "idle"]  # post 2 stored, then a pass with nothing new


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


async def test_run_forever_connects_with_backoff_then_polls(db) -> None:
    await ChannelPostsRepo(db).upsert_version(post(1), channel_id=CHANNEL, backfilled=True)
    reader = FakeReader([post(1)], connected=False, fail_connect=2)
    stopping = asyncio.Event()

    async def on_idle() -> None:
        stopping.set()

    poller = ChannelPoller(db, reader, channel_id=CHANNEL, recheck=0, on_idle=on_idle)
    assert poller.status() == {"connected": False, "last_ok": None, "last_error": None}
    await asyncio.wait_for(poller.run_forever(0.01, stopping, retry_seconds=0.001), timeout=5)

    assert reader.connects == 3  # two failures, then connected
    status = poller.status()
    assert status["connected"] is True and status["last_error"] is None
    assert datetime.fromisoformat(status["last_ok"]).tzinfo is not None


async def test_a_failed_connect_is_reported_and_retried(db) -> None:
    reader = FakeReader([], connected=False, fail_connect=-1)
    stopping = asyncio.Event()
    poller = ChannelPoller(db, reader, channel_id=CHANNEL, recheck=0)
    task = asyncio.create_task(poller.run_forever(0.05, stopping, retry_seconds=0.001))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if reader.connects >= 3:
            break
    stopping.set()
    await asyncio.wait_for(task, timeout=5)

    assert reader.connects >= 3
    assert poller.status() == {"connected": False, "last_ok": None, "last_error": "ConnectionError"}


async def test_run_forever_reconnects_after_a_drop(db) -> None:
    await ChannelPostsRepo(db).upsert_version(post(1), channel_id=CHANNEL, backfilled=True)
    reader = FakeReader([post(1)])
    stopping = asyncio.Event()
    passes: list[bool] = []

    async def on_idle() -> None:
        passes.append(reader.connected)
        if len(passes) == 1:
            reader.connected = False  # Telethon gave up its own reconnects
        else:
            stopping.set()

    poller = ChannelPoller(db, reader, channel_id=CHANNEL, recheck=0, on_idle=on_idle)
    await asyncio.wait_for(poller.run_forever(0.01, stopping, retry_seconds=0.001), timeout=5)

    assert passes == [True, True]
    assert reader.connects == 1


# --- the lifespan task --------------------------------------------------------------------


def boot_settings(**kwargs):
    from trader.ops.config import Settings

    return Settings(_env_file=None, auth_disabled=True, calls_channel_id=CHANNEL, **kwargs)


def lifespan_spies(monkeypatch, reader):
    import trader.calls.desk as desk_module
    import trader.surfaces.api.app as app_module

    runs: list[dict] = []

    class SpyPoller(ChannelPoller):
        async def run_forever(self, interval_seconds: float, stopping: asyncio.Event, **kw):
            runs.append(
                {
                    "interval": interval_seconds,
                    "task": asyncio.current_task().get_name(),
                    "on_new": self._on_new,
                    "on_idle": self._on_idle,
                }
            )
            try:
                await super().run_forever(interval_seconds, stopping, **kw)
            finally:  # shutdown sets ``stopping`` and cancels, as for settlement
                runs[-1]["ended"] = stopping.is_set()

    async def fake_reader_from_store(db, settings):
        return reader

    monkeypatch.setattr(app_module, "reader_from_store", fake_reader_from_store)
    monkeypatch.setattr(desk_module, "ChannelPoller", SpyPoller)
    return app_module, runs


def wait_for(condition, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_lifespan_connects_in_the_background_and_runs_the_poller(
    clean_db: None, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    reader = FakeReader([], connected=False)
    app_module, runs = lifespan_spies(monkeypatch, reader)
    s = boot_settings(calls_enabled=True, calls_poll_seconds=7.0)
    with TestClient(app_module.create_app(s)) as tc:
        assert tc.get("/health").status_code == 200
        wait_for(lambda: reader.connected)
        assert runs and runs[0]["interval"] == 7.0
        assert runs[0]["task"] == "trader-calls-poller"
        # Every pass, with posts stored or not, runs the desk's whole pipeline.
        assert runs[0]["on_new"] == runs[0]["on_idle"]
        assert runs[0]["on_idle"].__name__ == "process"
        card = tc.get("/calls/scorecard")
        assert card.status_code == 200
        assert card.json()["poller"]["connected"] is True
    assert runs[0].get("ended") is True
    assert reader.connected is False  # disconnected at shutdown


def test_lifespan_skips_the_poller_when_disabled(clean_db: None, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    reader = FakeReader([], connected=False)
    app_module, runs = lifespan_spies(monkeypatch, reader)
    with TestClient(app_module.create_app(boot_settings(calls_enabled=False))) as tc:
        assert tc.get("/health").status_code == 200
    assert runs == [] and reader.connects == 0


def test_lifespan_without_a_session_answers_409(clean_db: None, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    app_module, runs = lifespan_spies(monkeypatch, None)
    with TestClient(app_module.create_app(boot_settings(calls_enabled=True))) as tc:
        assert tc.get("/health").status_code == 200
        assert tc.get("/calls/scorecard").status_code == 409
    assert runs == []


def test_lifespan_with_telegram_down_reads_and_answers_503(clean_db: None, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    reader = FakeReader([], connected=False, fail_connect=-1)
    app_module, runs = lifespan_spies(monkeypatch, reader)
    with TestClient(app_module.create_app(boot_settings(calls_enabled=True))) as tc:
        assert tc.get("/health").status_code == 200  # boot does not wait for Telegram
        wait_for(lambda: tc.get("/calls/scorecard").json()["poller"]["last_error"] is not None)
        card = tc.get("/calls/scorecard")
        assert card.status_code == 200
        assert card.json()["poller"] == {
            "connected": False,
            "last_ok": None,
            "last_error": "ConnectionError",
        }
        assert tc.get("/calls").status_code == 200
        assert tc.get("/calls/posts").status_code == 200
        for path in ("/calls/poll", "/calls/backfill"):
            resp = tc.post(path)
            assert resp.status_code == 503, (path, resp.text)
            assert "not connected" in resp.json()["detail"]
    assert runs and runs[0].get("ended") is True
