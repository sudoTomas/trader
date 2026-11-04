"""Earnings blackout (#141): no new opens or adds on a stock around its earnings report.

From ``earnings_blackout_days`` sessions before the report through the report session,
RiskGuard denies an open or add on the symbol (verdict ``earnings_blackout``). A report
before the open moves that session; one after the close moves the next, so the window
covers both: the sessions before carry the position into the report, the report session
itself is the gap. Exits never reach this check: closes, trims and the stop always pass.

The report date is a fact the OpenBB producer wrote (``openbb_snapshots``, kind
``earnings``), read here as data, the way the stop cooldown reads the journal. It is not
research: no model wrote it, and nothing here imports ``trader.research``.

No calendar must not freeze trading: with no row for the symbol, or a row older than
``EARNINGS_STALE_AFTER``, the check **fails open**: the open proceeds, a warning is
logged, and the allow verdict says why, so the decision's journaled risk verdicts carry it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from trader.core.symbols import AssetClass, Symbol
from trader.store.db import Database
from trader.store.repos import OpenBBRepo

EARNINGS_KIND = "earnings"  # openbb_snapshots.kind; loop.sources.EARNINGS
#: The producer refreshes several times a day; three days covers a long weekend's gap.
EARNINGS_STALE_AFTER = timedelta(days=3)


@dataclass(frozen=True)
class EarningsCalendar:
    """The newest earnings row for one symbol, as the blackout reads it."""

    version: int  # openbb_snapshots.id
    fetched_at: datetime
    report_date: date | None  # the next report the row knew of; None = none in its window


@dataclass(frozen=True)
class BlackoutCheck:
    blocked: bool
    reason: str
    failed_open: bool = False  # no usable calendar: allowed, and the reason says so


def applies(symbol: Symbol) -> bool:
    """Only US stocks report earnings (an ETF's calendar row is simply empty)."""
    return symbol.asset_class is AssetClass.US_EQUITY


async def load_earnings(db: Database, ticker: str, as_of: datetime) -> EarningsCalendar | None:
    """The newest earnings row fetched at or before ``as_of`` (replay-safe)."""
    row = await OpenBBRepo(db).latest(EARNINGS_KIND, ticker, as_of=as_of)
    if row is None:
        return None
    raw = (row.get("data") or {}).get("next_report_date")
    try:
        report = date.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        report = None
    return EarningsCalendar(version=row["id"], fetched_at=row["ts"], report_date=report)


def _window_end(symbol: Symbol, now: datetime, days: int) -> date:
    """The session ``days`` sessions after the one at ``now``."""
    calendar = symbol.market_calendar
    at = now
    for _ in range(days):
        close = calendar.next_close(at)
        if close is None:  # a venue that never closes has no sessions to count
            break
        at = calendar.next_open(close)
    return calendar.session_day(at)


def earnings_blackout(
    earnings: EarningsCalendar | None, symbol: Symbol, now: datetime, days: int
) -> BlackoutCheck:
    """Whether an open or add on ``symbol`` at ``now`` falls in its blackout window.

    Blocked when the report date lies between this session and the session ``days``
    sessions ahead, inclusive (so with ``days=1``: the session before and the report
    session itself)."""
    if earnings is None:
        return BlackoutCheck(
            False, f"no earnings calendar for {symbol.ticker}: blackout not applied", True
        )
    age = now - earnings.fetched_at
    if age > EARNINGS_STALE_AFTER:
        return BlackoutCheck(
            False,
            f"earnings calendar #{earnings.version} is stale (fetched"
            f" {earnings.fetched_at.isoformat()}): blackout not applied",
            True,
        )
    if earnings.report_date is None:
        return BlackoutCheck(False, f"no report scheduled (calendar #{earnings.version})")
    today = symbol.market_calendar.session_day(now)
    last = _window_end(symbol, now, days)
    if today <= earnings.report_date <= last:
        return BlackoutCheck(
            True,
            f"earnings on {earnings.report_date.isoformat()} (calendar #{earnings.version}):"
            f" no opens or adds from {days} session(s) before through the report session",
        )
    return BlackoutCheck(
        False, f"next report {earnings.report_date.isoformat()} (calendar #{earnings.version})"
    )
