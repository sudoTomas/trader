"""Channel poller: stores the configured channel's posts and their edits as versions.

Each pass reads the posts after the newest stored ``msg_id`` (paged), then re-reads the
newest ``recheck`` posts for edits; an edit is stored as version n+1 with its post's
``backfilled`` flag, and never touches ``call_parses`` (a live call keeps its first parse).
Every write is idempotent, so a pass that fails half way is simply redone by the next one.

Until the backfill has stored something the poller stores nothing: posts read before it
would be stored as live and count toward the live weeks of the gate.

``run_forever`` owns the Telegram connection: it connects before its first pass (retrying
with backoff, so a boot never waits on Telegram) and reconnects before any pass that
finds the reader disconnected (Telethon gives up its own reconnects after a while).
``status`` reports the connection, the last good pass and the last error type.

Logs carry counts and errors only, never post text or anything from the session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from trader.calls.reader import ChannelReader
from trader.store.db import Database
from trader.store.repos import ChannelPostsRepo

log = structlog.get_logger(__name__)

#: Posts asked of ``newer`` per page.
PAGE = 200
#: The first wait after a failed connect; it doubles per failure up to the poll interval.
RETRY_SECONDS = 5.0


class ChannelPoller:
    def __init__(
        self,
        db: Database,
        reader: ChannelReader,
        *,
        channel_id: int,
        recheck: int,
        on_new: Callable[[], Awaitable[object]] | None = None,
        on_idle: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._posts = ChannelPostsRepo(db)
        self._reader = reader
        self._channel_id = channel_id
        self._recheck = recheck
        self._on_new = on_new
        self._on_idle = on_idle
        # A poll during a backfill would store the history ahead of it as live posts.
        self._lock = asyncio.Lock()
        self._last_ok: datetime | None = None
        self._last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._reader.is_connected()

    def status(self) -> dict[str, Any]:
        """``connected``; ``last_ok``, when the last background pass succeeded (ISO, UTC);
        ``last_error``, the error type of the last failed connect or pass (None once a
        pass succeeds again)."""
        return {
            "connected": self.connected,
            "last_ok": self._last_ok.isoformat() if self._last_ok else None,
            "last_error": self._last_error,
        }

    async def poll_once(self) -> dict[str, int]:
        """Store new posts and edits of the newest ``recheck``; ``{"new": n, "edited": m}``."""
        async with self._lock:
            newest = await self._posts.max_msg_id(self._channel_id)
            if newest == 0:
                log.info("calls_poll_waiting_for_backfill", channel_id=self._channel_id)
                return {"new": 0, "edited": 0}
            new = 0
            while True:
                page = await self._reader.newer(self._channel_id, newest, limit=PAGE)
                for p in page:
                    if await self._posts.upsert_version(
                        p, channel_id=self._channel_id, backfilled=False
                    ):
                        new += 1
                    newest = max(newest, p.msg_id)
                if len(page) < PAGE:
                    break
            edited = 0
            if self._recheck > 0:
                for p in await self._reader.latest(self._channel_id, self._recheck):
                    first = await self._posts.first(self._channel_id, p.msg_id)
                    if first is None:  # not stored: new posts come only from ``newer``
                        continue
                    if await self._posts.upsert_version(
                        p, channel_id=self._channel_id, backfilled=first["backfilled"]
                    ):
                        edited += 1
            return {"new": new, "edited": edited}

    async def backfill(self) -> dict[str, int]:
        """Store every post in the channel's history as backfilled; ``{"stored": n}``."""
        async with self._lock:
            stored = 0
            async for p in self._reader.history(self._channel_id):
                if await self._posts.upsert_version(
                    p, channel_id=self._channel_id, backfilled=True
                ):
                    stored += 1
            log.info("calls_backfill_stored", channel_id=self._channel_id, stored=stored)
            return {"stored": stored}

    async def run_forever(
        self,
        interval_seconds: float,
        stopping: asyncio.Event,
        *,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        """A poll every ``interval_seconds`` until ``stopping``; a failed pass is logged.
        Before each pass, a disconnected reader is connected; a failed connect waits
        ``retry_seconds``, doubling per failure up to ``interval_seconds``, and tries again.
        ``on_new`` runs after a pass that stored anything, ``on_idle`` after one that
        stored nothing."""
        backoff = retry_seconds
        while not stopping.is_set():
            if not await self._connect():
                await _sleep(stopping, min(backoff, interval_seconds))
                backoff = min(backoff * 2, interval_seconds)
                continue
            backoff = retry_seconds
            try:
                counts = await self.poll_once()
                self._last_ok, self._last_error = datetime.now(UTC), None
                if counts["new"] or counts["edited"]:
                    log.info("calls_poll_stored", **counts)
                    if self._on_new is not None:
                        await self._on_new()
                elif self._on_idle is not None:
                    await self._on_idle()
            except Exception as exc:  # noqa: BLE001 - polling must never take the process down
                # The error type only: a Telegram error's text could quote the channel.
                self._last_error = type(exc).__name__
                log.warning("calls_poll_failed", error=self._last_error)
            await _sleep(stopping, interval_seconds)

    async def _connect(self) -> bool:
        """Connect the reader unless it is connected; whether it is now."""
        if self._reader.is_connected():
            return True
        try:
            await self._reader.connect()
        except Exception as exc:  # noqa: BLE001 - a dead login or network: retried
            self._last_error = type(exc).__name__
            log.warning("calls_connect_failed", error=self._last_error)
            return False
        log.info("calls_connected", channel_id=self._channel_id)
        return True


async def _sleep(stopping: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or less when ``stopping`` is set."""
    try:
        await asyncio.wait_for(stopping.wait(), timeout=seconds)
    except TimeoutError:
        pass
