"""Account breakers and kill switches — DB-backed so a restart can never un-halt trading.

Two breakers, both measured on **equity** (marks included, not realized-only: unrealized
bleed is still bleed):

- ``daily_loss`` — equity down more than ``daily_loss_limit_frac`` from the session day's
  opening equity. The window rolls on the venue's session day (core/calendars), so it
  never rolls mid-session.
- ``weekly_drawdown`` — equity down more than ``weekly_drawdown_limit_frac`` off the
  rolling high-water.

A breach halts **new opens only**; exits stay managed (a halt that trapped positions
would be a bigger risk than the one it's guarding). Operator resume clears the halt AND
re-baselines the high-water — without that, a monotonic high-water re-trips the kill on
the very next tick (v1:engine/sleeve.py::resume).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import structlog

from trader.store.db import Database

log = structlog.get_logger(__name__)

DAILY_LOSS = "daily_loss"
WEEKLY_DRAWDOWN = "weekly_drawdown"


@dataclass(frozen=True)
class RiskState:
    """The persisted kill-switch/breaker snapshot the guard chain reads."""

    trading_paused: bool = False
    pause_reason: str | None = None
    drift_detected: bool = False
    halted_reason: str | None = None
    day_session: date | None = None
    day_start_equity: float | None = None
    week_high_equity: float | None = None

    @property
    def blocked_reason(self) -> str | None:
        """Why new opens are blocked right now, if they are."""
        if self.trading_paused:
            return f"trading paused: {self.pause_reason or 'operator'}"
        if self.drift_detected:
            return "position drift detected between broker and local ledger"
        if self.halted_reason:
            return f"breaker halted: {self.halted_reason}"
        return None


def _to_float(value) -> float | None:
    return None if value is None else float(value)


class BreakerStore:
    """Owns ``risk_state``. All mutations go through here."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self) -> RiskState:
        row = await self._db.fetch_one("SELECT * FROM risk_state WHERE id = 1")
        if row is None:  # migration guarantees the row; treat absence as fail-closed
            log.error("risk_state_missing")
            return RiskState(trading_paused=True, pause_reason="risk_state row missing")
        return RiskState(
            trading_paused=row["trading_paused"],
            pause_reason=row["pause_reason"],
            drift_detected=row["drift_detected"],
            halted_reason=row["halted_reason"],
            day_session=row["day_session"],
            day_start_equity=_to_float(row["day_start_equity"]),
            week_high_equity=_to_float(row["week_high_equity"]),
        )

    async def _update(self, **fields) -> None:
        assignments = ", ".join(f"{name} = %s" for name in fields)
        await self._db.execute(
            f"UPDATE risk_state SET {assignments}, updated_at = now() WHERE id = 1",
            tuple(fields.values()),
        )

    async def pause(self, reason: str) -> None:
        await self._update(trading_paused=True, pause_reason=reason)
        log.warning("trading_paused", reason=reason)

    async def unpause(self) -> None:
        await self._update(trading_paused=False, pause_reason=None)
        log.info("trading_unpaused")

    async def set_drift(self, detected: bool) -> None:
        await self._update(drift_detected=detected)
        log.warning("drift_flag_set", detected=detected)

    async def resume(self, equity: float) -> None:
        """Operator resume: clear the halt and re-baseline both windows to ``equity``."""
        await self._update(
            halted_reason=None,
            halted_at=None,
            week_high_equity=equity,
            day_start_equity=equity,
        )
        log.info("breakers_resumed", equity=equity)

    async def observe(
        self,
        *,
        equity: float,
        session_day: date,
        daily_loss_limit_frac: float,
        weekly_drawdown_limit_frac: float,
    ) -> RiskState:
        """Record current equity, roll the day window, and trip breakers on breach.

        Called once per wakeup before the guard chain runs; returns the fresh state.
        """
        state = await self.load()

        # Roll the session day: a new day re-baselines the daily window and clears a
        # daily-loss halt (the drawdown halt persists — it is not a daily concept).
        if state.day_session != session_day:
            halted = None if state.halted_reason == DAILY_LOSS else state.halted_reason
            await self._update(
                day_session=session_day, day_start_equity=equity, halted_reason=halted
            )
            state = await self.load()

        if state.week_high_equity is None or equity > state.week_high_equity:
            await self._update(week_high_equity=equity)
            state = await self.load()

        if state.halted_reason:  # already halted; nothing further to trip
            return state

        day_start = state.day_start_equity
        if day_start and (equity - day_start) / day_start <= -daily_loss_limit_frac:
            return await self._halt(DAILY_LOSS, equity=equity, baseline=day_start)

        high = state.week_high_equity
        if high and (equity - high) / high <= -weekly_drawdown_limit_frac:
            return await self._halt(WEEKLY_DRAWDOWN, equity=equity, baseline=high)

        return state

    async def _halt(self, reason: str, *, equity: float, baseline: float) -> RiskState:
        await self._db.execute(
            "UPDATE risk_state SET halted_reason = %s, halted_at = now(),"
            " updated_at = now() WHERE id = 1",
            (reason,),
        )
        log.error("breaker_tripped", reason=reason, equity=equity, baseline=baseline)
        return await self.load()
