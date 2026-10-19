"""Sleeves (#125, ADR 0003): the share of the one account each Bot trades.

A Sleeve is a Bot's capital and the positions its own fills built (``bot_positions``).
It is *data* for the one RiskGuard chain, never a second checker (see
docs/lessons.md#risk-chain-consolidated): the shell loads it here and hands it to
``evaluate_chain`` beside the account.

- **Capital** is a fixed dollar amount in the Bot's spec (``sleeve.capital_usd``), or
  null for a whole-account Sleeve (Bot #1): its equity is the account's, and its
  breakers are the account's, so it has none of its own (only a pause).
- A fixed Sleeve's **equity** is capital + realized P&L + its holdings marked to the
  prices at hand (a holding without a price counts at its entry). Sizing, Kelly and the
  Bot's own breakers read it.
- Rows with no Bot (written before Bots, or by a build that names none) are Bot #1's.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from trader.core.symbols import Symbol
from trader.risk.breakers import BreakerStore, RiskState
from trader.risk.limits import RiskLimits
from trader.store.db import Database
from trader.store.repos import BotPositionsRepo

#: The Bot an unattributed row belongs to: the agent that ran before Bots existed.
UNATTRIBUTED_BOT_ID = 1


@dataclass(frozen=True)
class Sleeve:
    """One Bot's share of the account, as the guard chain reads it."""

    bot_id: int | None
    equity: float  # what sizing and Kelly read; the account's for a whole-account Sleeve
    positions: dict[str, float] = field(default_factory=dict)  # ticker -> signed qty
    trades_today: int = 0
    state: RiskState = field(default_factory=RiskState)  # the Bot's own pause / breakers
    capital: float | None = None  # None = the whole account


def sleeve_capital(spec: Mapping[str, Any]) -> float | None:
    """The fixed dollar capital in a Bot spec; None = the whole account. Anything
    unreadable is NaN, which the chain's input sanity denies."""
    raw = (spec.get("sleeve") or {}).get("capital_usd")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return math.nan


def sleeve_equity(
    capital: float, rows: Iterable[Mapping[str, Any]], prices: Mapping[str, float]
) -> float:
    """Capital + realized P&L + each holding marked at ``prices`` (unpriced: at entry)."""
    equity = capital
    for row in rows:
        equity += float(row.get("realized_pnl") or 0.0)
        qty = float(row["qty"])
        entry = row.get("avg_entry_price")
        price = prices.get(row["symbol"])
        if qty and entry is not None and price is not None:
            equity += qty * (float(price) - float(entry))
    return equity


def session_window(symbol: Symbol, now: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` covering the venue session day that contains ``now``.

    Crypto sessions are UTC dates; NYSE sessions are America/New_York dates.
    ``session_day`` never crosses local midnight, so midnight-to-midnight is the
    whole daily trade window.
    """
    day = symbol.market_calendar.session_day(now)
    tz = ZoneInfo("America/New_York") if symbol.market_calendar.name == "nyse" else UTC
    start = datetime.combine(day, dt_time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


async def count_trades_today(
    db: Database, symbol: Symbol, now: datetime, *, bot_id: int | None = None
) -> int:
    """Orders already recorded in this symbol's session day: the account's, or with
    ``bot_id`` that Bot's.

    Every order row counts, including rejects: a failed submit still consumed an
    attempt, and the cap is there to limit churn. Shared by the wakeup path and the
    approval-redeem door so both see the same daily cap.
    """
    start, end = session_window(symbol, now)
    sql = "SELECT count(*) AS n FROM orders WHERE created_at >= %s AND created_at < %s"
    params: tuple[Any, ...] = (start, end)
    if bot_id is not None:
        sql += " AND coalesce(bot_id, %s) = %s"
        params += (UNATTRIBUTED_BOT_ID, bot_id)
    row = await db.fetch_one(sql, params)
    return int(row["n"]) if row else 0


class SleeveLedger:
    """Loads a Bot's Sleeve for the guard, and observes its breakers."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._positions = BotPositionsRepo(db)

    async def _spec(self, bot_id: int) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT v.spec FROM bots b JOIN bot_versions v"
            " ON v.bot_id = b.id AND v.version = b.current_version WHERE b.id = %s",
            (bot_id,),
        )
        return row["spec"] if row else None

    async def capital(self, bot_id: int) -> float | None:
        spec = await self._spec(bot_id)
        return sleeve_capital(spec) if spec is not None else None

    async def equity(
        self, bot_id: int, *, account_equity: float, prices: Mapping[str, float]
    ) -> float:
        """The Sleeve's equity now: the account's for a whole-account Sleeve."""
        capital = await self.capital(bot_id)
        if capital is None:
            return account_equity
        return sleeve_equity(capital, await self._positions.for_bot(bot_id), prices)

    async def load(
        self,
        bot_id: int,
        *,
        account_equity: float,
        symbol: Symbol,
        price: float,
        now: datetime,
        limits: RiskLimits,
    ) -> Sleeve:
        spec = await self._spec(bot_id)
        if spec is None:  # no version describes this Bot: fail closed
            reason = f"bot {bot_id} has no current version"
            return Sleeve(
                bot_id, account_equity, state=RiskState(trading_paused=True, pause_reason=reason)
            )
        capital = sleeve_capital(spec)
        rows = await self._positions.for_bot(bot_id)
        positions = {r["symbol"]: float(r["qty"]) for r in rows if float(r["qty"]) != 0}
        store = BreakerStore(self._db, bot_id=bot_id)
        if capital is None:
            equity = account_equity
            state = await store.load()  # its breakers are the account's
        else:
            equity = sleeve_equity(capital, rows, {symbol.ticker: price})
            state = await self._observe(
                store, equity, symbol.market_calendar.session_day(now), limits
            )
        return Sleeve(
            bot_id=bot_id,
            equity=equity,
            positions=positions,
            trades_today=await count_trades_today(self._db, symbol, now, bot_id=bot_id),
            state=state,
            capital=capital,
        )

    async def observe(
        self,
        bot_id: int,
        *,
        session_day: date,
        prices: Mapping[str, float],
        limits: RiskLimits,
    ) -> None:
        """Roll a fixed Sleeve's session and trip its breakers off marked equity (the
        tick does this so holds keep the windows fresh). A whole-account Sleeve has
        nothing of its own to observe."""
        capital = await self.capital(bot_id)
        if capital is None or not math.isfinite(capital):
            return
        equity = sleeve_equity(capital, await self._positions.for_bot(bot_id), prices)
        await self._observe(BreakerStore(self._db, bot_id=bot_id), equity, session_day, limits)

    @staticmethod
    async def _observe(
        store: BreakerStore, equity: float, session_day: date, limits: RiskLimits
    ) -> RiskState:
        if not math.isfinite(equity) or equity <= 0:
            return await store.load()  # the chain denies an unreadable Sleeve anyway
        return await store.observe(
            equity=equity,
            session_day=session_day,
            daily_loss_limit_frac=limits.daily_loss_limit_frac,
            weekly_drawdown_limit_frac=limits.weekly_drawdown_limit_frac,
        )
