"""CallsDesk: the channel calls' one service object, over the poller, parser, linker and
scorer, and the call scorecard every surface reads.

``process`` is the pipeline after posts are stored: parse what is pending, link the
updates, score the calls that have no score yet, and rescore (at most hourly) the open
ones and those whose bars fetch failed. The desk owns one poller over the reader, and
``run`` drives it: every pass, new posts or not, ends in ``process``, so posts a spend
cap or a model error left unparsed are picked up without waiting for a new post.
``poll`` and ``backfill`` are the manual commands; ``backfill`` returns once the history
is stored and processes it in the background. Bars are fetched per call and never
stored. Phase 1 places no orders: nothing here reaches the executor, the broker or
RiskGuard.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trader.calls.link import link_updates
from trader.calls.parse import CallLevels, CallParser, CallUpdate
from trader.calls.poller import ChannelPoller
from trader.calls.reader import ChannelReader
from trader.calls.score import CallScore, _unscorable, simulate, size_usd
from trader.core.symbols import AssetClass, make_symbol
from trader.eval.costs import costs_from_settings
from trader.ops.config import Settings
from trader.store.db import Database
from trader.store.repos import CallParsesRepo, CallScoresRepo, ChannelPostsRepo, Row

log = structlog.get_logger(__name__)

#: Bars are fetched from this long before the post, so the entry bar is in the window.
BARS_LEAD = timedelta(minutes=15)
#: A call younger than this with no entry bar yet is left unscored, not unscorable.
ENTRY_GRACE = timedelta(hours=1)
#: The reason prefix of a call unscorable because its bars fetch raised or came back
#: empty. A fetch that raised (an outage) is retried every ``RESCORE_EVERY``.
BARS_UNAVAILABLE = "bars unavailable"
#: The reason prefix of an empty fetch past ``ENTRY_GRACE``, or of bars that all predate
#: the post: the coin has no bars at Alpaca. Stable: only a backfill rescores it.
NO_BARS = f"{BARS_UNAVAILABLE}: no bars"
#: An open call, or one whose bars fetch raised, is rescored at most this often: bars
#: come from the trading loop's Alpaca keys, and an open call moves slowly on 15m bars.
RESCORE_EVERY = timedelta(hours=1)
#: Posts parsed per ``parse_pending`` batch.
PARSE_BATCH = 50
#: The gate's live period, in days since the first live post.
GATE_LIVE_DAYS = 28
#: The gate's share of resolved tradeable calls that reach TP1 before the stop.
GATE_TP1_RATE = 0.5


class CallsOff(RuntimeError):
    """The calls capabilities were asked for in a build without a CallsDesk."""

    def __init__(self) -> None:
        super().__init__("Telegram calls are off or no session is stored")


class CallsDisconnected(RuntimeError):
    """A command that reads Telegram while the poller has not (re)connected."""

    def __init__(self) -> None:
        super().__init__(
            "Telegram is not connected yet; the poller keeps retrying (see the scorecard's"
            " poller). The stored posts, calls and scorecard still answer."
        )


class CallsDesk:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        market: Any | None,
        llm: Any,
        reader: ChannelReader | None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._market = market
        self._channel_id = settings.calls_channel_id
        # ONE poller: the background passes and the manual commands share its lock.
        self._poller = (
            None
            if reader is None
            else ChannelPoller(
                db,
                reader,
                channel_id=self._channel_id,
                recheck=settings.calls_recheck_posts,
                on_new=self.process,
                on_idle=self.process,  # open calls move with the bars, not only with posts
            )
        )
        self._processing: asyncio.Task | None = None
        self._parser = CallParser(db, llm, channel_id=self._channel_id)
        self._posts = ChannelPostsRepo(db)
        self._parses = CallParsesRepo(db)
        self._scores = CallScoresRepo(db)
        # The background poller and a manual command must not parse the same post twice.
        self._lock = asyncio.Lock()

    def _require_poller(self) -> ChannelPoller:
        if self._poller is None:
            raise CallsOff()
        if not self._poller.connected:
            raise CallsDisconnected()
        return self._poller

    async def run(self, stopping: asyncio.Event) -> None:
        """The background poller, every ``calls_poll_seconds`` until ``stopping``."""
        if self._poller is None:
            raise CallsOff()
        await self._poller.run_forever(self._settings.calls_poll_seconds, stopping)

    async def aclose(self) -> None:
        """Cancel a background ``process`` (shutdown); its writes are idempotent."""
        task, self._processing = self._processing, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def process(self, *, rescore: bool = False) -> dict[str, Any]:
        """Parse until nothing is pending (or the model fails), link, score."""
        async with self._lock:
            parsed: dict[str, int] = {}
            while True:
                counts = await self._parser.parse_pending(PARSE_BATCH)
                for kind, n in counts.items():
                    parsed[kind] = parsed.get(kind, 0) + n
                # A short batch: nothing left, or a model error stopped it.
                if sum(counts.values()) < PARSE_BATCH:
                    break
            linked = await link_updates(self._db, self._channel_id)
            scored = await self._score(rescore=rescore)
            out = {"parsed": parsed, "linked": linked, "scored": scored}
            log.info("calls_process_done", channel_id=self._channel_id, rescore=rescore, **out)
            return out

    async def poll(self) -> dict[str, Any]:
        """One poll now, then ``process``."""
        counts = await self._require_poller().poll_once()
        return {"poll": counts, **await self.process()}

    async def backfill(self) -> dict[str, Any]:
        """Store the channel's whole history and return; ``process`` with every call
        rescored runs as a desk-owned background task (a parse of the whole history can
        outlast any client's timeout). ``processing``: ``started``, or ``already running``
        when an earlier backfill's is (the poller's next pass parses what it misses)."""
        counts = await self._require_poller().backfill()
        if self._processing is not None and not self._processing.done():
            return {"backfill": counts, "processing": "already running"}
        self._processing = asyncio.create_task(
            self._process_in_background(rescore=True), name="trader-calls-process"
        )
        return {"backfill": counts, "processing": "started"}

    async def _process_in_background(self, *, rescore: bool) -> None:
        try:
            await self.process(rescore=rescore)
        except Exception as exc:  # noqa: BLE001 - logged; the poller's next pass resumes it
            log.warning("calls_process_failed", error=type(exc).__name__)

    async def _score(self, *, rescore: bool) -> int:
        if self._market is None:
            log.warning("calls_score_skipped", reason="no market data in this build")
            return 0
        existing = {s["msg_id"]: s for s in await self._scores.all(self._channel_id)}
        now = datetime.now(UTC)
        stored = 0
        for call in await self._parses.calls(self._channel_id):
            if not rescore and not _due(existing.get(call["msg_id"]), now):
                continue
            try:
                if await self._score_one(call):
                    stored += 1
            except Exception as exc:  # noqa: BLE001 - one bad call must not stop the batch
                # Only a failed write lands here; the next pass retries it.
                log.warning("calls_score_failed", msg_id=call["msg_id"], error=type(exc).__name__)
        return stored

    async def _score_one(self, call: Row) -> bool:
        try:
            levels = CallLevels.model_validate(call["payload"]["call"])
        except Exception:  # noqa: BLE001 - a stored payload that no longer validates
            await self._scores.put(
                self._channel_id,
                call["msg_id"],
                status="unscorable",
                tradeable=False,
                result={"status": "unscorable", "reason": "invalid levels"},
            )
            return True
        try:
            score = await self._simulate(call, levels)
        except Exception as exc:  # noqa: BLE001 - stored as a result, never retried forever
            log.warning("calls_score_failed", msg_id=call["msg_id"], error=type(exc).__name__)
            score = _unscorable(levels, f"score failed: {type(exc).__name__}")
        if score is None:
            return False
        await self._scores.put(
            self._channel_id,
            call["msg_id"],
            status=score.status,
            # Tradeable: a long with bars to enter on (an unscorable call never counts).
            tradeable=levels.side == "long" and score.status != "unscorable",
            result=score.as_dict(),
        )
        return True

    async def _simulate(self, call: Row, levels: CallLevels) -> CallScore | None:
        """The call's score, or None when it is too fresh for its entry bar."""
        posted_at = call["posted_at"].astimezone(UTC)
        now = datetime.now(UTC)
        bars: list = []
        reason: str | None = None
        try:
            symbol = make_symbol(f"{levels.coin}/USD", AssetClass.CRYPTO)
            bars = await self._market.get_bars_between(symbol, "15m", posted_at - BARS_LEAD, now)
        except Exception as exc:  # noqa: BLE001 - an unlisted coin is a result, not an error
            reason = f"{BARS_UNAVAILABLE}: {type(exc).__name__}"
        if reason is None and not bars:
            if now - posted_at < ENTRY_GRACE:
                return None  # the entry bar has not printed yet
            reason = f"{NO_BARS} for {levels.coin}/USD"
        if reason is not None:
            score = _unscorable(levels, reason)
        else:
            updates = [
                (u["posted_at"].astimezone(UTC), CallUpdate.model_validate(u["payload"]["update"]))
                for u in await self._parses.updates_for(self._channel_id, call["msg_id"])
            ]
            score = simulate(
                levels,
                posted_at,
                updates,
                bars,
                stop_frac=self._settings.risk_stop_loss_frac,  # fixed, never vol-tightened
                costs=costs_from_settings(self._settings),
                asset_class=AssetClass.CRYPTO,
            )
            if score.status == "unscorable":  # every bar predates the post
                if now - posted_at < ENTRY_GRACE:
                    return None  # the entry bar has not printed yet
                score = replace(score, reason=f"{NO_BARS} after post for {levels.coin}/USD")
        return score

    async def scorecard(self) -> dict[str, Any]:
        """The call scorecard. ``tp1_rate`` is ``tp1_before_stop`` over the *resolved*
        tradeable calls (first_touch tp1 or stop): an open call that has touched neither
        is not a miss yet. P&L and ``net_return_mean`` are over closed tradeable calls."""
        calls = await self._parses.calls(self._channel_id)
        scores = {s["msg_id"]: s for s in await self._scores.all(self._channel_id)}
        sleeve = self._settings.calls_sleeve_usd

        tradeable = open_ = unscorable = closed = tp1 = resolved = 0
        sleeve_pnl = 0.0
        nets: list[float] = []
        other = {"calls": 0, "nets": []}
        coins: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "tradeable": 0, "closed": 0, "tp1_before_stop": 0, "nets": []}
        )
        for call in calls:
            try:
                levels = CallLevels.model_validate(call["payload"]["call"])
            except Exception:  # noqa: BLE001 - scored "invalid levels", counted as such
                levels = None
            coin = coins[levels.coin if levels is not None else "?"]
            coin["calls"] += 1
            score = scores.get(call["msg_id"])
            if score is None:
                continue
            status, result = score["status"], score["result"]
            open_ += status == "open"
            unscorable += status == "unscorable"
            closed += status == "closed"
            if not score["tradeable"]:
                other["calls"] += 1
                if status == "closed":
                    other["nets"].append(result["net_return"])
                continue
            tradeable += 1
            coin["tradeable"] += 1
            if result.get("first_touch", "neither") != "neither":
                resolved += 1
            if result.get("first_touch") == "tp1":
                tp1 += 1
                coin["tp1_before_stop"] += 1
            if status == "closed" and levels is not None:
                net = result["net_return"]
                nets.append(net)
                coin["closed"] += 1
                coin["nets"].append(net)
                sleeve_pnl += net * size_usd(levels, sleeve)

        live_days = await self._live_days()
        backfilled = any(c["backfilled"] for c in calls)
        tp1_rate = tp1 / resolved if resolved else 0.0
        reasons: list[str] = []
        if not backfilled:
            reasons.append("no backfilled calls yet: run the backfill")
        if live_days < GATE_LIVE_DAYS:
            reasons.append(f"{live_days} live days of the {GATE_LIVE_DAYS} needed")
        if sleeve_pnl <= 0:
            reasons.append(f"sleeve net P&L ${sleeve_pnl:,.2f} is not positive")
        if tp1_rate < GATE_TP1_RATE:
            reasons.append(
                f"{tp1} of {resolved} resolved tradeable calls reached TP1 before the stop"
            )
        return {
            "calls": len(calls),
            "tradeable": tradeable,
            "scored": closed + open_,
            "open": open_,
            "unscorable": unscorable,
            "tp1_before_stop": tp1,
            "tp1_rate": tp1_rate,
            "net_return_mean": _mean(nets),
            "sleeve_pnl_usd": round(sleeve_pnl, 2),
            "by_coin": {
                name: {
                    **{k: v for k, v in c.items() if k != "nets"},
                    "net_return_mean": _mean(c["nets"]),
                }
                for name, c in sorted(coins.items())
            },
            "untradeable": {"calls": other["calls"], "net_return_mean": _mean(other["nets"])},
            "live_days": live_days,
            "gate_pass": not reasons,
            "gate_reasons": reasons,
            "poller": self._poller.status() if self._poller is not None else None,
        }

    async def _live_days(self) -> int:
        row = await self._db.fetch_one(
            "SELECT MIN(posted_at) AS first FROM channel_posts"
            " WHERE channel_id = %s AND NOT backfilled",
            (self._channel_id,),
        )
        if row is None or row["first"] is None:
            return 0
        return (datetime.now(UTC) - row["first"]).days

    async def calls(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Calls newest first: levels, flags, score and linked updates."""
        scores = {s["msg_id"]: s for s in await self._scores.all(self._channel_id)}
        out = []
        for call in reversed((await self._parses.calls(self._channel_id))[-limit:]):
            score = scores.get(call["msg_id"])
            updates = await self._parses.updates_for(self._channel_id, call["msg_id"])
            out.append(
                {
                    "msg_id": call["msg_id"],
                    "posted_at": call["posted_at"].isoformat(),
                    "backfilled": call["backfilled"],
                    "levels": call["payload"]["call"],
                    "flags": call["payload"].get("flags", []),
                    "score": None
                    if score is None
                    else {
                        "status": score["status"],
                        "tradeable": score["tradeable"],
                        "result": score["result"],
                    },
                    "updates": [
                        {
                            "msg_id": u["msg_id"],
                            "posted_at": u["posted_at"].isoformat(),
                            "update": u["payload"]["update"],
                        }
                        for u in updates
                    ],
                }
            )
        return out

    async def posts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The latest version of each post, newest first, with its parse kind."""
        out = []
        for post in await self._posts.listing(self._channel_id, limit=limit):
            parse = await self._parses.get(self._channel_id, post["msg_id"])
            out.append(
                {
                    "msg_id": post["msg_id"],
                    "version": post["version"],
                    "posted_at": post["posted_at"].isoformat(),
                    "edited_at": post["edited_at"].isoformat() if post["edited_at"] else None,
                    "reply_to": post["reply_to"],
                    "text": post["text"],
                    "media": post["media"],
                    "backfilled": post["backfilled"],
                    "kind": parse["kind"] if parse is not None else None,
                }
            )
        return out


def _due(score: Row | None, now: datetime) -> bool:
    """Whether a call's stored score is due a rescore on a normal pass: never scored;
    or, at most every ``RESCORE_EVERY``, open or unscorable because its bars fetch
    raised. Closed, no bars (``NO_BARS``) and other unscorable rows wait for a backfill."""
    if score is None:
        return True
    if now - score["scored_at"] < RESCORE_EVERY:
        return False
    if score["status"] == "open":
        return True
    reason = score["result"].get("reason") or ""
    return (
        score["status"] == "unscorable"
        and reason.startswith(BARS_UNAVAILABLE)
        and not reason.startswith(NO_BARS)
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
