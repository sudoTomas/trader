"""CallsDesk: the channel calls' one service object, over the poller, parser, linker and
scorer, and the call scorecard every surface reads.

``process`` is the pipeline after posts are stored: parse what is pending, link the
updates, score the calls that have no score yet or are still ``open``. The background
poller runs it after a pass that stored something; ``poll`` and ``backfill`` are the
manual commands. Bars are fetched per call and never stored. Phase 1 places no orders:
nothing here reaches the executor, the broker or RiskGuard.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trader.calls.link import link_updates
from trader.calls.parse import CallLevels, CallParser, CallUpdate
from trader.calls.poller import ChannelPoller
from trader.calls.score import _unscorable, simulate, size_usd
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
#: Posts parsed per ``parse_pending`` batch.
PARSE_BATCH = 50
#: The gate's live period, in days since the first live post.
GATE_LIVE_DAYS = 28
#: The gate's share of tradeable calls that reach TP1 before the stop.
GATE_TP1_RATE = 0.5


class CallsOff(RuntimeError):
    """The calls capabilities were asked for in a build without a CallsDesk."""

    def __init__(self) -> None:
        super().__init__("Telegram calls are off or no session is stored")


class CallsDesk:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        market: Any | None,
        llm: Any,
        poller: ChannelPoller | None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._market = market
        self._poller = poller
        self._channel_id = settings.calls_channel_id
        self._parser = CallParser(db, llm, channel_id=self._channel_id)
        self._posts = ChannelPostsRepo(db)
        self._parses = CallParsesRepo(db)
        self._scores = CallScoresRepo(db)
        # The background poller and a manual command must not parse the same post twice.
        self._lock = asyncio.Lock()

    def _require_poller(self) -> ChannelPoller:
        if self._poller is None:
            raise CallsOff()
        return self._poller

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
            return {"parsed": parsed, "linked": linked, "scored": scored}

    async def poll(self) -> dict[str, Any]:
        """One poll now, then ``process``."""
        counts = await self._require_poller().poll_once()
        return {"poll": counts, **await self.process()}

    async def backfill(self) -> dict[str, Any]:
        """Store the channel's whole history, then ``process`` and rescore every call."""
        counts = await self._require_poller().backfill()
        return {"backfill": counts, **await self.process(rescore=True)}

    async def score_pending(self) -> int:
        """Score calls with no score or status ``open``; the number stored."""
        async with self._lock:
            return await self._score(rescore=False)

    async def _score(self, *, rescore: bool) -> int:
        if self._market is None:
            log.warning("calls_score_skipped", reason="no market data in this build")
            return 0
        existing = {s["msg_id"]: s["status"] for s in await self._scores.all(self._channel_id)}
        stored = 0
        for call in await self._parses.calls(self._channel_id):
            status = existing.get(call["msg_id"])
            if not rescore and status is not None and status != "open":
                continue
            try:
                if await self._score_one(call):
                    stored += 1
            except Exception as exc:  # noqa: BLE001 - one bad call must not stop the batch
                log.warning("calls_score_failed", msg_id=call["msg_id"], error=type(exc).__name__)
        return stored

    async def _score_one(self, call: Row) -> bool:
        levels = CallLevels.model_validate(call["payload"]["call"])
        posted_at = call["posted_at"].astimezone(UTC)
        now = datetime.now(UTC)
        bars: list = []
        reason: str | None = None
        try:
            symbol = make_symbol(f"{levels.coin}/USD", AssetClass.CRYPTO)
            bars = await self._market.get_bars_between(symbol, "15m", posted_at - BARS_LEAD, now)
        except Exception as exc:  # noqa: BLE001 - an unlisted coin is a result, not an error
            reason = f"bars unavailable: {type(exc).__name__}"
        if reason is None and not bars:
            reason = f"no bars for {levels.coin}/USD"
        if reason is not None:
            if now - posted_at < ENTRY_GRACE and not reason.startswith("bars unavailable"):
                return False
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
            if score.status == "unscorable" and now - posted_at < ENTRY_GRACE:
                return False  # the entry bar has not printed yet
        await self._scores.put(
            self._channel_id,
            call["msg_id"],
            status=score.status,
            # Tradeable: a long with bars to enter on (an unscorable call never counts).
            tradeable=levels.side == "long" and score.status != "unscorable",
            result=score.as_dict(),
        )
        return True

    async def scorecard(self) -> dict[str, Any]:
        calls = await self._parses.calls(self._channel_id)
        scores = {s["msg_id"]: s for s in await self._scores.all(self._channel_id)}
        sleeve = self._settings.calls_sleeve_usd

        tradeable = open_ = unscorable = closed = tp1 = 0
        sleeve_pnl = 0.0
        nets: list[float] = []
        other = {"calls": 0, "nets": []}
        coins: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "tradeable": 0, "closed": 0, "tp1_before_stop": 0, "nets": []}
        )
        for call in calls:
            levels = CallLevels.model_validate(call["payload"]["call"])
            coin = coins[levels.coin]
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
            if result.get("first_touch") == "tp1":
                tp1 += 1
                coin["tp1_before_stop"] += 1
            if status == "closed":
                net = result["net_return"]
                nets.append(net)
                coin["closed"] += 1
                coin["nets"].append(net)
                sleeve_pnl += net * size_usd(levels, sleeve)

        live_days = await self._live_days()
        backfilled = any(c["backfilled"] for c in calls)
        tp1_rate = tp1 / tradeable if tradeable else 0.0
        reasons: list[str] = []
        if not backfilled:
            reasons.append("no backfilled calls yet: run the backfill")
        if live_days < GATE_LIVE_DAYS:
            reasons.append(f"{live_days} live days of the {GATE_LIVE_DAYS} needed")
        if sleeve_pnl <= 0:
            reasons.append(f"sleeve net P&L ${sleeve_pnl:,.2f} is not positive")
        if tp1_rate < GATE_TP1_RATE:
            reasons.append(f"{tp1} of {tradeable} tradeable calls reached TP1 before the stop")
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


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
