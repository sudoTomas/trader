"""Bots (#123): one trading strategy each, versioned append-only like Profiles.

A Bot is the symbols it trades (each with its Symbol mode), the triggers it wakes on,
the Sources it reads (#124), its prompt, the account it trades on, and its Sleeve's
capital (#125: a fixed dollar amount, or null for the whole account). Every change to
that spec
appends a Bot version and moves the Bot's current pointer; a journaled decision names
the Bot and the Bot version that made it.

For now there is one Bot: the agent that ran before Bots existed. Its spec is derived
from Settings and seeded at boot, so a Settings change becomes a new, audited version on
the next start and an unchanged one writes nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from psycopg.types.json import Jsonb

from trader.loop.decider import SYSTEM_PROMPT
from trader.loop.scheduler import TriggerConfig
from trader.loop.sources import bot_one_subscriptions
from trader.ops.config import Settings
from trader.risk.breakers import BreakerStore
from trader.risk.sleeves import sleeve_capital
from trader.store.audit import CALLER, SURFACE, audit
from trader.store.db import Database
from trader.store.repos import BotPositionsRepo

#: The agent that ran before Bots existed (the migration creates it as id 1).
BOT_ONE = "bot-1"
#: Every Bot carries its account, so a Bot on its own paper account later is
#: configuration, not a schema change (ADR 0003).
ALPACA_PAPER = "alpaca-paper"
LIVE = "live"
SHADOW = "shadow"


class BotNotFound(LookupError):
    """No Bot by that name."""


def trigger_config(settings: Settings) -> TriggerConfig:
    """The triggers the loop wakes on, from Settings (the runner's scheduler uses this)."""
    return TriggerConfig(
        max_heartbeats_per_tick=settings.loop_max_heartbeats_per_tick,
        sentiment_enabled=settings.sentiment_trigger_enabled,
        sentiment_swing=settings.sentiment_swing_threshold,
    )


def bot_one_spec(settings: Settings) -> dict[str, Any]:
    """Bot #1's spec as the running build would trade it. JSON-shaped, so it compares
    equal to what ``bot_versions.spec`` stores."""
    tickers = [t.strip() for t in settings.loop_symbols.split(",") if t.strip()]
    shadowed = {t.strip() for t in settings.shadow_symbols.split(",") if t.strip()}
    triggers = asdict(trigger_config(settings))
    triggers["heartbeat_seconds"] = {
        str(asset_class): seconds for asset_class, seconds in triggers["heartbeat_seconds"].items()
    }
    spec = {
        "account_id": ALPACA_PAPER,
        "symbols": {t: SHADOW if settings.shadow_mode or t in shadowed else LIVE for t in tickers},
        "triggers": triggers,
        # The Sources it reads (#124), from the lane flags it had before Sources.
        "sources": bot_one_subscriptions(
            briefs=settings.briefs_enabled, exit_review=settings.exit_review_enabled
        ),
        # The desk is not a Source: it reads the finished context, not a producer's rows.
        "lanes": {"desk": settings.desk_enabled},
        # The decide rules are code, so a change to them is a new version too.
        "prompt": {
            "variant": "default",
            "sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        },
        # Bot #1's Sleeve is the whole account (ADR 0003): it sizes against account
        # equity, so its verdicts are what they were before Sleeves.
        "sleeve": {"capital_usd": None},
    }
    return json.loads(json.dumps(spec))


class BotDesk:
    """Every Bot read, and the boot-time seed."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            """
            SELECT b.id, b.name, b.current_version AS version, v.spec
            FROM bots b JOIN bot_versions v
              ON v.bot_id = b.id AND v.version = b.current_version
            ORDER BY b.id
            """
        )
        return [await self._with_sleeve(self._out(r)) for r in rows]

    async def get(self, name: str) -> dict[str, Any]:
        """One Bot with every version, newest first."""
        bot = await self._bot(name)
        if bot is None or bot["version"] is None:
            raise BotNotFound(name)
        versions = await self._db.fetch_all(
            "SELECT version, spec, created_at FROM bot_versions"
            " WHERE bot_id = %s ORDER BY version DESC",
            (bot["id"],),
        )
        current = next(v for v in versions if v["version"] == bot["version"])
        return {
            **await self._with_sleeve(self._out({**bot, "spec": current["spec"]})),
            "versions": [
                {
                    "version": v["version"],
                    "spec": v["spec"],
                    "created_at": v["created_at"].isoformat(),
                }
                for v in versions
            ],
        }

    async def seed(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Make ``spec`` the Bot's current version: a new version only when it differs
        from the current one. Returns the Bot as ``list`` shows it, less its Sleeve."""
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO bots (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,)
            )
            # The Bot's own pause and breakers (#125); a missing row is fail-closed.
            await conn.execute(
                "INSERT INTO risk_state (scope) SELECT 'bot:' || id FROM bots WHERE name = %s"
                " ON CONFLICT (scope) DO NOTHING",
                (name,),
            )
            cur = await conn.execute(
                """
                SELECT b.id, b.current_version, v.spec
                FROM bots b LEFT JOIN bot_versions v
                  ON v.bot_id = b.id AND v.version = b.current_version
                WHERE b.name = %s
                FOR UPDATE OF b
                """,
                (name,),
            )
            bot = await cur.fetchone()
            assert bot is not None
            if bot["spec"] == spec:
                return self._out(
                    {"id": bot["id"], "name": name, "version": bot["current_version"], "spec": spec}
                )
            cur = await conn.execute(
                """
                INSERT INTO bot_versions (bot_id, version, spec)
                SELECT %s, coalesce(max(version), 0) + 1, %s FROM bot_versions WHERE bot_id = %s
                RETURNING version
                """,
                (bot["id"], Jsonb(spec), bot["id"]),
            )
            row = await cur.fetchone()
            assert row is not None
            await conn.execute(
                "UPDATE bots SET current_version = %s, updated_at = now() WHERE id = %s",
                (row["version"], bot["id"]),
            )
        await audit(
            self._db,
            actor=CALLER.get(),
            category="bots",
            action="seed_bot_version",
            subject=name,
            payload={"version": row["version"], "spec": spec, "surface": SURFACE.get()},
        )
        return self._out({"id": bot["id"], "name": name, "version": row["version"], "spec": spec})

    async def _bot(self, name: str) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT id, name, current_version AS version FROM bots WHERE name = %s", (name,)
        )

    async def _with_sleeve(self, bot: dict[str, Any]) -> dict[str, Any]:
        """The Bot's Sleeve: its capital, what its fills built, and its own risk state."""
        state = await BreakerStore(self._db, bot_id=bot["id"]).load()
        rows = await BotPositionsRepo(self._db).for_bot(bot["id"])
        return {
            **bot,
            "sleeve": {
                "capital_usd": sleeve_capital(bot["spec"]),
                "positions": [
                    {
                        "symbol": r["symbol"],
                        "qty": float(r["qty"]),
                        "avg_entry_price": (
                            float(r["avg_entry_price"])
                            if r["avg_entry_price"] is not None
                            else None
                        ),
                        "realized_pnl": float(r["realized_pnl"]),
                    }
                    for r in rows
                    if float(r["qty"]) != 0 or float(r["realized_pnl"]) != 0
                ],
                "risk": {
                    "trading_paused": state.trading_paused,
                    "pause_reason": state.pause_reason,
                    "halted_reason": state.halted_reason,
                    "blocked_reason": state.blocked_reason,
                },
            },
        }

    @staticmethod
    def _out(row: dict[str, Any]) -> dict[str, Any]:
        spec = row["spec"]
        return {
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "account_id": spec.get("account_id"),
            "spec": spec,
        }
