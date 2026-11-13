"""Channel poller: stores the configured channel's posts and their edits as versions.

Each pass reads the posts after the newest stored ``msg_id`` (paged), then re-reads the
newest ``recheck`` posts for edits; an edit is stored as version n+1 with its post's
``backfilled`` flag, and never touches ``call_parses`` (a live call keeps its first parse).
Every write is idempotent, so a pass that fails half way is simply redone by the next one.

Until the backfill has stored something the poller stores nothing: posts read before it
would be stored as live and count toward the live weeks of the gate.

Logs carry counts and errors only, never post text or anything from the session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from trader.calls.reader import ChannelReader
from trader.store.db import Database
from trader.store.repos import ChannelPostsRepo

log = structlog.get_logger(__name__)

#: Posts asked of ``newer`` per page.
PAGE = 200


class ChannelPoller:
    def __init__(
        self,
        db: Database,
        reader: ChannelReader,
        *,
        channel_id: int,
        recheck: int,
        on_new: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._posts = ChannelPostsRepo(db)
        self._reader = reader
        self._channel_id = channel_id
        self._recheck = recheck
        self._on_new = on_new
        # A poll during a backfill would store the history ahead of it as live posts.
        self._lock = asyncio.Lock()

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
            log.info("calls_backfill_done", channel_id=self._channel_id, stored=stored)
            return {"stored": stored}

    async def run_forever(self, interval_seconds: float, stopping: asyncio.Event) -> None:
        """A poll every ``interval_seconds`` until ``stopping``; a failed pass is logged.
        ``on_new`` runs after a pass that stored anything."""
        while not stopping.is_set():
            try:
                counts = await self.poll_once()
                if counts["new"] or counts["edited"]:
                    log.info("calls_poll_stored", **counts)
                    if self._on_new is not None:
                        await self._on_new()
            except Exception as exc:  # noqa: BLE001 - polling must never take the process down
                # The error type only: a Telegram error's text could quote the channel.
                log.warning("calls_poll_failed", error=type(exc).__name__)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
