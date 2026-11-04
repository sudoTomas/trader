"""Point in time (#142): a replayed decision sees only what was public at its bar.

Every Source slice carries an **availability time**: the instant the newest thing it
says became public (a view's compile, a poll, a filing's acceptance, a daily print's end
of day). Replay pins each read to ``as_of``; this module is the one rule every Source is
held to, wherever its data came from:

- **clamp** — a read asked for a moment past ``as_of`` (a context clock that ran ahead,
  a window that ends later) is served as of ``as_of``, never answered past it;
- **visible** — an item is served only when its availability time is at or before
  ``as_of``; an undated item is withheld, since replay cannot prove it is not from later;
- **PointInTime** — the guard the ContextBuilder puts around every Source: it clamps
  the read and refuses a slice dated after ``as_of``, so a Source with a bug in its own
  as-of filter drops its lane instead of leaking the future into a prompt.

Live reads (``as_of=None``) pass through unchanged: nothing the live loop reads can be
from after now. Some data carries its own dates (SEC filings), so a row fetched later
still serves the past honestly item by item; data with no historical vintage
(Polymarket odds, StockTwits/Reddit counts, X sentiment) is available from the moment
it was fetched, so replay sees it only for bars after the producer ran. The idea is
TradingAgents' ``dataflows/date_window.py`` (Apache-2.0); this is our own implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger(__name__)

NEW_YORK = ZoneInfo("America/New_York")


def clamp(requested: datetime | None, as_of: datetime | None) -> datetime | None:
    """The instant a read serves: the request, never later than ``as_of``."""
    if as_of is None:
        return requested
    if requested is None or requested > as_of:
        return as_of
    return requested


def visible(available_at: datetime | None, as_of: datetime | None) -> bool:
    """Whether an item available at ``available_at`` may be served as of ``as_of``."""
    if as_of is None:
        return True
    return available_at is not None and available_at <= as_of


def latest_visible[T](
    items: Iterable[T],
    as_of: datetime | None,
    *,
    available_at: Callable[[T], datetime | None],
    order: Callable[[T], Any] | None = None,
) -> T | None:
    """The newest item visible as of ``as_of`` (``order`` breaks ties; default: the
    availability time alone)."""
    eligible = [item for item in items if visible(available_at(item), as_of)]
    if not eligible:
        return None
    return max(eligible, key=order or available_at)  # type: ignore[arg-type]


def day_end(day: date, tz: ZoneInfo = NEW_YORK) -> datetime:
    """The first instant a print dated ``day`` can be known: the next midnight in ``tz``.

    A daily close is not public before its day ends, so a step at 11:00 must not read
    that day's print. A lower bound: some series publish later still (FRED's VIX close
    lands the next morning, monthly series weeks after their period).
    """
    return datetime.combine(day + timedelta(days=1), time(), tzinfo=tz)


class PointInTime:
    """A Source held to the as-of rule. Quacks like the Source it wraps."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name

    async def read(self, symbol: Any, *, as_of: datetime | None, params: Mapping[str, Any]) -> Any:
        if as_of is None:
            return await self.inner.read(symbol, as_of=None, params=params)
        clamped = {**params, "now": clamp(params.get("now"), as_of)}
        piece = await self.inner.read(symbol, as_of=as_of, params=clamped)
        if piece is None:
            return None
        available_at = getattr(piece, "available_at", None)
        if not visible(available_at, as_of):
            log.warning(
                "source_lookahead_refused",
                source=self.name,
                symbol=getattr(symbol, "ticker", symbol),
                as_of=as_of.isoformat(),
                available_at=available_at.isoformat() if available_at else None,
            )
            return None
        return piece


def point_in_time(source: Any) -> PointInTime:
    """``source`` under the as-of rule (idempotent)."""
    return source if isinstance(source, PointInTime) else PointInTime(source)
