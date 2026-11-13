"""Link each ``update`` parse to the call it is about.

The reply chain wins: follow ``reply_to`` through the posts (at most ``MAX_DEPTH`` hops,
which also ends a reply cycle) until a post whose parse is a ``call``. Without one, the
newest call on the same coin posted within ``WINDOW`` before the update. Otherwise the
update stays unlinked. Times are the ``posted_at`` of each post's first version.

A call that gains an update loses its stored score, so the next scoring pass simulates
it with the update (a closed score is otherwise never rescored).
"""

from __future__ import annotations

from datetime import timedelta

from trader.store.db import Database
from trader.store.repos import CallParsesRepo, CallScoresRepo, ChannelPostsRepo, Row

#: Reply hops followed before giving up on the chain.
MAX_DEPTH = 10
#: How far before an update the same-coin fallback looks for its call.
WINDOW = timedelta(days=30)


async def link_updates(db: Database, channel_id: int) -> int:
    """Set ``links_to`` on every unlinked update that resolves to a call; the number linked."""
    parses = CallParsesRepo(db)
    posts = ChannelPostsRepo(db)
    scores = CallScoresRepo(db)
    calls = await parses.calls(channel_id)
    linked = 0
    for update in await parses.unlinked_updates(channel_id):
        call_id = await _by_reply(parses, posts, channel_id, update["reply_to"])
        if call_id is None:
            call_id = _by_coin(calls, update)
        if call_id is not None:
            # Score dropped first: a failure between the two leaves the update unlinked,
            # and the next pass links it and drops the score again.
            await scores.drop(channel_id, call_id)
            await parses.set_link(channel_id, update["msg_id"], call_id)
            linked += 1
    return linked


async def _by_reply(
    parses: CallParsesRepo, posts: ChannelPostsRepo, channel_id: int, reply_to: int | None
) -> int | None:
    for _ in range(MAX_DEPTH):
        if reply_to is None:
            return None
        parse = await parses.get(channel_id, reply_to)
        if parse is not None and parse["kind"] == "call":
            return reply_to
        post = await posts.first(channel_id, reply_to)
        if post is None:
            return None
        reply_to = post["reply_to"]
    return None


def _by_coin(calls: list[Row], update: Row) -> int | None:
    coin = (update["payload"].get("update") or {}).get("coin")
    if not coin:
        return None
    at = update["posted_at"]
    for call in reversed(calls):  # newest first
        call_coin = (call["payload"].get("call") or {}).get("coin") or ""
        if call_coin.upper() == coin.upper() and at - WINDOW <= call["posted_at"] <= at:
            return call["msg_id"]
    return None
