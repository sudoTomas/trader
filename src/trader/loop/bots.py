"""Bots (#123): one trading strategy each, versioned append-only like Profiles.

A Bot is the symbols it trades (each with its Symbol mode), the triggers it wakes on,
the Sources it reads (#124), its prompt, the account it trades on, and its Sleeve's
capital (#125: a fixed dollar amount, or null for the whole account). Every change to
that spec appends a Bot version and moves the Bot's current pointer; a journaled
decision names the Bot and the Bot version that made it.

Bot #1 is the agent that ran before Bots existed. Its spec is derived from Settings and
seeded at boot, so a Settings change becomes a new, audited version on the next start
and an unchanged one writes nothing; the version commands refuse it. Every other Bot is
defined in the store only (#126): created, re-versioned and moved between Symbol modes
through the commands here, every change audited.

The Owner rule: one Bot only may be live on a symbol (ADR 0003). Any write that would
make a second Bot live on it is refused, under one lock so two writes cannot race past
each other.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, fields
from typing import Any

from psycopg.types.json import Jsonb

from trader.core.symbols import AssetClass
from trader.loop.decider import DEFAULT_VARIANT, PROMPT_VARIANTS, prompt_spec
from trader.loop.scheduler import TriggerConfig
from trader.loop.sources import (
    BRIEF,
    FRED,
    HELD_POSITION,
    MACRO_VIEW,
    SENTIMENT,
    bot_one_subscriptions,
    subscriptions,
)
from trader.ops.config import Settings, get_settings
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
ACCOUNTS = frozenset({ALPACA_PAPER})
REPLAY = "replay"
LIVE = "live"
SHADOW = "shadow"
SYMBOL_MODES = (REPLAY, SHADOW, LIVE)
#: Every Source a Bot may subscribe to (loop/sources.build_sources wires each one).
SOURCE_NAMES = frozenset({MACRO_VIEW, BRIEF, FRED, SENTIMENT, HELD_POSITION})
# Any constant: the transaction-scoped advisory lock every Owner check takes.
_OWNER_LOCK = 126


class BotNotFound(LookupError):
    """No Bot by that name."""


class BotSpecError(ValueError):
    """A Bot spec or change the desk refuses (bad field, Owner conflict, Bot #1)."""


def trigger_config(settings: Settings) -> TriggerConfig:
    """The triggers the loop wakes on, from Settings (the runner's scheduler uses this)."""
    return TriggerConfig(
        max_heartbeats_per_tick=settings.loop_max_heartbeats_per_tick,
        sentiment_enabled=settings.sentiment_trigger_enabled,
        sentiment_swing=settings.sentiment_swing_threshold,
    )


def triggers_spec(config: TriggerConfig) -> dict[str, Any]:
    """A TriggerConfig as a spec stores it (asset classes as strings)."""
    triggers = asdict(config)
    triggers["heartbeat_seconds"] = {
        str(asset_class): seconds for asset_class, seconds in triggers["heartbeat_seconds"].items()
    }
    return triggers


def triggers_from_spec(triggers: Mapping[str, Any]) -> TriggerConfig:
    """The inverse of ``triggers_spec``: what a Bot's scheduler wakes on."""
    values = dict(triggers)
    if "heartbeat_seconds" in values:
        values["heartbeat_seconds"] = {
            AssetClass(k): int(v) for k, v in values["heartbeat_seconds"].items()
        }
    return TriggerConfig(**values)


def bot_one_spec(settings: Settings) -> dict[str, Any]:
    """Bot #1's spec as the running build would trade it. JSON-shaped, so it compares
    equal to what ``bot_versions.spec`` stores."""
    tickers = [t.strip() for t in settings.loop_symbols.split(",") if t.strip()]
    shadowed = {t.strip() for t in settings.shadow_symbols.split(",") if t.strip()}
    spec = {
        "account_id": ALPACA_PAPER,
        "symbols": {t: SHADOW if settings.shadow_mode or t in shadowed else LIVE for t in tickers},
        "triggers": triggers_spec(trigger_config(settings)),
        # The Sources it reads (#124), from the lane flags it had before Sources.
        "sources": bot_one_subscriptions(
            briefs=settings.briefs_enabled, exit_review=settings.exit_review_enabled
        ),
        # The desk is not a Source: it reads the finished context, not a producer's rows.
        "lanes": {"desk": settings.desk_enabled},
        # The decide rules are code, so a change to them is a new version too.
        "prompt": prompt_spec(DEFAULT_VARIANT),
        # Bot #1's Sleeve is the whole account (ADR 0003): it sizes against account
        # equity, so its verdicts are what they were before Sleeves.
        "sleeve": {"capital_usd": None},
    }
    return json.loads(json.dumps(spec))


def bot_spec(
    *,
    symbols: Mapping[str, str],
    capital_usd: float,
    sources: Iterable[str | Mapping[str, Any]] = (),
    prompt_variant: str = DEFAULT_VARIANT,
    triggers: Mapping[str, Any] | None = None,
    account_id: str = ALPACA_PAPER,
    desk: bool = False,
) -> dict[str, Any]:
    """A store-defined Bot's spec, checked. Bot #1's shape, with a fixed-dollar Sleeve.

    ``triggers`` overrides Bot #1's triggers field by field; ``sources`` names Sources
    (or ``{source, params}`` subscriptions). Every symbol must be in this build's
    universe (``TRADER_EXTRA_SYMBOLS`` mints stocks). Raises BotSpecError.
    """
    universe = get_settings().symbol_registry()
    if not symbols:
        raise BotSpecError("a Bot needs at least one symbol")
    for ticker, mode in symbols.items():
        try:
            universe.get(ticker)
        except KeyError as exc:
            raise BotSpecError(str(exc.args[0])) from None
        if mode not in SYMBOL_MODES:
            raise BotSpecError(f"{ticker}: Symbol mode must be one of {SYMBOL_MODES}, not {mode!r}")
    subs = subscriptions(
        {"source": s} if isinstance(s, str) else {"source": s.get("source"), **s} for s in sources
    )
    unknown = sorted(str(n) for n in {s.source for s in subs} - SOURCE_NAMES)
    if unknown:
        raise BotSpecError(f"unknown Sources {unknown} (known: {sorted(SOURCE_NAMES)})")
    if prompt_variant not in PROMPT_VARIANTS:
        raise BotSpecError(
            f"unknown prompt variant {prompt_variant!r} (known: {sorted(PROMPT_VARIANTS)})"
        )
    known = {f.name for f in fields(TriggerConfig)}
    bad = sorted(set(triggers or {}) - known)
    if bad:
        raise BotSpecError(f"unknown trigger settings {bad} (known: {sorted(known)})")
    try:
        trigger_values = {**triggers_spec(trigger_config(get_settings())), **(triggers or {})}
        triggers_from_spec(trigger_values)
    except (TypeError, ValueError) as exc:
        raise BotSpecError(f"unreadable triggers: {exc}") from None
    try:
        capital = float(capital_usd)
    except (TypeError, ValueError):
        raise BotSpecError("capital_usd must be a number") from None
    if not math.isfinite(capital) or capital <= 0:
        raise BotSpecError("capital_usd must be a positive dollar amount")
    if account_id not in ACCOUNTS:
        raise BotSpecError(f"unknown account {account_id!r} (known: {sorted(ACCOUNTS)})")
    spec = {
        "account_id": account_id,
        "symbols": dict(symbols),
        "triggers": trigger_values,
        "sources": [s.as_spec() for s in subs],
        "lanes": {"desk": bool(desk)},
        "prompt": prompt_spec(prompt_variant),
        "sleeve": {"capital_usd": capital},
    }
    return json.loads(json.dumps(spec))


def live_symbols(spec: Mapping[str, Any]) -> set[str]:
    return {t for t, mode in (spec.get("symbols") or {}).items() if mode == LIVE}


class BotDesk:
    """Every Bot read and write: the boot-time seed of Bot #1, and the commands that
    create and re-version the others."""

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
            bot = await self._locked(conn, name)
            assert bot is not None
            if bot["spec"] == spec:
                return self._out(
                    {"id": bot["id"], "name": name, "version": bot["current_version"], "spec": spec}
                )
            # Settings can name a symbol live that another Bot owns: refuse the boot.
            await self._check_owner(conn, bot["id"], spec)
            version = await self._append(conn, bot["id"], spec)
        await self._audit("seed_bot_version", name, {"version": version, "spec": spec})
        return self._out({"id": bot["id"], "name": name, "version": version, "spec": spec})

    async def create(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        """A new store-defined Bot at version 1 (``spec`` from ``bot_spec``)."""
        name = name.strip()
        if not name:
            raise BotSpecError("a Bot needs a name")
        async with self._db.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO bots (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id",
                (name,),
            )
            row = await cur.fetchone()
            if row is None:
                raise BotSpecError(f"a Bot named {name!r} already exists")
            await conn.execute(
                "INSERT INTO risk_state (scope) VALUES (%s) ON CONFLICT (scope) DO NOTHING",
                (f"bot:{row['id']}",),
            )
            await self._check_owner(conn, row["id"], spec)
            version = await self._append(conn, row["id"], spec)
        await self._audit("create_bot", name, {"version": version, "spec": spec})
        return self._out({"id": row["id"], "name": name, "version": version, "spec": spec})

    async def new_version(
        self,
        name: str,
        *,
        symbols: Mapping[str, str] | None = None,
        sources: Iterable[str | Mapping[str, Any]] | None = None,
        prompt_variant: str | None = None,
        triggers: Mapping[str, Any] | None = None,
        capital_usd: float | None = None,
    ) -> dict[str, Any]:
        """The Bot's current spec with the given fields replaced (``triggers`` merges
        into the current ones), as a new version. An unchanged spec writes nothing."""
        return await self._revise(
            name,
            "new_bot_version",
            lambda spec: bot_spec(
                symbols=spec["symbols"] if symbols is None else symbols,
                capital_usd=spec["sleeve"]["capital_usd"] if capital_usd is None else capital_usd,
                sources=spec["sources"] if sources is None else sources,
                prompt_variant=(
                    spec["prompt"]["variant"] if prompt_variant is None else prompt_variant
                ),
                triggers={**spec["triggers"], **(triggers or {})},
                account_id=spec["account_id"],
                desk=spec.get("lanes", {}).get("desk", False),
            ),
        )

    async def set_symbol_mode(self, name: str, symbol: str, mode: str) -> dict[str, Any]:
        """Move one of the Bot's symbols to replay, shadow or live, as a new version.
        Live refuses when another Bot is already live on it (the Owner rule)."""

        def change(spec: dict[str, Any]) -> dict[str, Any]:
            if symbol not in spec["symbols"]:
                raise BotSpecError(f"{name} does not trade {symbol}; add it with a new version")
            if mode not in SYMBOL_MODES:
                raise BotSpecError(f"Symbol mode must be one of {SYMBOL_MODES}, not {mode!r}")
            return {**spec, "symbols": {**spec["symbols"], symbol: mode}}

        return await self._revise(name, "set_symbol_mode", change)

    async def _revise(self, name: str, action: str, change: Any) -> dict[str, Any]:
        if name == BOT_ONE:
            raise BotSpecError(
                f"{BOT_ONE} follows Settings: change TRADER_LOOP_SYMBOLS, TRADER_SHADOW_SYMBOLS"
                " or its lane flags and restart"
            )
        async with self._db.connection() as conn:
            bot = await self._locked(conn, name)
            if bot is None or bot["spec"] is None:
                raise BotNotFound(name)
            spec = change(bot["spec"])
            if spec == bot["spec"]:
                return self._out(
                    {"id": bot["id"], "name": name, "version": bot["current_version"], "spec": spec}
                )
            await self._check_owner(conn, bot["id"], spec)
            version = await self._append(conn, bot["id"], spec)
        await self._audit(
            action, name, {"version": version, "from_version": bot["current_version"], "spec": spec}
        )
        return self._out({"id": bot["id"], "name": name, "version": version, "spec": spec})

    @staticmethod
    async def _locked(conn: Any, name: str) -> dict[str, Any] | None:
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
        return await cur.fetchone()

    @staticmethod
    async def _check_owner(conn: Any, bot_id: int, spec: Mapping[str, Any]) -> None:
        """The Owner rule: refuse ``spec`` if it is live on a symbol another Bot's
        current version is live on. The lock serialises every check until commit."""
        wanted = live_symbols(spec)
        if not wanted:
            return
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_OWNER_LOCK,))
        cur = await conn.execute(
            """
            SELECT b.name, v.spec FROM bots b JOIN bot_versions v
              ON v.bot_id = b.id AND v.version = b.current_version
            WHERE b.id <> %s
            """,
            (bot_id,),
        )
        for other in await cur.fetchall():
            taken = sorted(wanted & live_symbols(other["spec"]))
            if taken:
                raise BotSpecError(
                    f"{', '.join(taken)} already live on {other['name']} (its Owner);"
                    " one live Bot per symbol"
                )

    @staticmethod
    async def _append(conn: Any, bot_id: int, spec: dict[str, Any]) -> int:
        cur = await conn.execute(
            """
            INSERT INTO bot_versions (bot_id, version, spec)
            SELECT %s, coalesce(max(version), 0) + 1, %s FROM bot_versions WHERE bot_id = %s
            RETURNING version
            """,
            (bot_id, Jsonb(spec), bot_id),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.execute(
            "UPDATE bots SET current_version = %s, updated_at = now() WHERE id = %s",
            (row["version"], bot_id),
        )
        return row["version"]

    async def _audit(self, action: str, name: str, payload: dict[str, Any]) -> None:
        await audit(
            self._db,
            actor=CALLER.get(),
            category="bots",
            action=action,
            subject=name,
            payload={**payload, "surface": SURFACE.get()},
        )

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
