"""Market session calendars. Pure, no I/O — imported everywhere.

Adapted from the hardened v1 implementation (v1:engine/market_calendar.py), minus the CME
Globex calendar (futures/IBKR are out of scope for v2). Semantics preserved:

- Everything is computed in the exchange's IANA zone via ``zoneinfo`` (never fixed UTC
  offsets), so DST transitions shift the UTC open/close instants automatically.
- All methods REQUIRE tz-aware datetimes and raise ``ValueError`` on naive input.
- The open instant is INCLUSIVE, the close instant EXCLUSIVE
  (``is_open(09:30) is True``, ``is_open(16:00) is False``).
- ``next_open``/``next_close`` return UTC tz-aware datetimes; ``next_open`` on an already
  open instant returns the input unchanged; ``next_close`` is ``None`` for venues that
  never close.
- ``session_day`` is the venue's trading-day bucket (daily-loss windows): defined at every
  instant, never rolls mid-session, always rolls between sessions.

HOLIDAY DATA — minimal fixed lists for 2026 (extension point): the constructor accepts
``holidays=`` / ``half_days=`` overrides, so extending coverage means appending future
years to the module constants or swapping in an exchange-calendar dependency behind the
same parameters. Past the listed years the weekday/hour logic still holds; the failure
mode is "open on a holiday", which the broker itself rejects.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

#: Days to scan forward for the next open/close. Covers the longest possible closed
#: stretch (holiday cluster + weekend) with ample headroom.
_SCAN_DAYS = 30


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("market calendar methods require a timezone-aware datetime")


@runtime_checkable
class MarketCalendar(Protocol):
    """Session calendar for one trading venue."""

    name: str

    def is_open(self, dt: datetime) -> bool:
        """True when the venue is trading at instant ``dt``."""
        ...

    def next_open(self, dt: datetime) -> datetime:
        """Earliest instant >= ``dt`` at which the venue is open (UTC).

        Returns ``dt`` itself when already open."""
        ...

    def next_close(self, dt: datetime) -> datetime | None:
        """End of the current session when open, else end of the next session (UTC).
        ``None`` for venues that never close."""
        ...

    def session_day(self, dt: datetime) -> date:
        """The venue's trading-day bucket for ``dt`` (daily-loss windows)."""
        ...


class AlwaysOpenCalendar:
    """Crypto: 24/7, never closes. ``session_day`` is the UTC calendar date."""

    name = "always_open"

    def is_open(self, dt: datetime) -> bool:
        _require_aware(dt)
        return True

    def next_open(self, dt: datetime) -> datetime:
        _require_aware(dt)
        return dt

    def next_close(self, dt: datetime) -> datetime | None:
        _require_aware(dt)
        return None

    def session_day(self, dt: datetime) -> date:
        _require_aware(dt)
        return dt.astimezone(UTC).date()


#: NYSE full-closure holidays, keyed by ET calendar date.
NYSE_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # MLK Day
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day (observed; Jul 4 is a Saturday)
        date(2026, 9, 7),  # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
    }
)

#: NYSE half days (early close time), keyed by ET calendar date.
NYSE_HALF_DAYS_2026: dict[date, time] = {
    date(2026, 11, 27): time(13, 0),  # Day after Thanksgiving
    date(2026, 12, 24): time(13, 0),  # Christmas Eve
}


class NYSECalendar:
    """Mon-Fri 09:30-16:00 America/New_York (13:00 close on half days)."""

    name = "nyse"

    _OPEN = time(9, 30)
    _CLOSE = time(16, 0)

    def __init__(
        self,
        holidays: Iterable[date] | None = None,
        half_days: dict[date, time] | None = None,
    ) -> None:
        self._tz = ZoneInfo("America/New_York")
        self._holidays = frozenset(NYSE_HOLIDAYS_2026 if holidays is None else holidays)
        self._half_days = dict(NYSE_HALF_DAYS_2026 if half_days is None else half_days)

    def _is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self._holidays

    def _close_time(self, day: date) -> time:
        return self._half_days.get(day, self._CLOSE)

    def is_open(self, dt: datetime) -> bool:
        _require_aware(dt)
        local = dt.astimezone(self._tz)
        day = local.date()
        return self._is_trading_day(day) and self._OPEN <= local.time() < self._close_time(day)

    def next_open(self, dt: datetime) -> datetime:
        _require_aware(dt)
        if self.is_open(dt):
            return dt
        local = dt.astimezone(self._tz)
        for offset in range(_SCAN_DAYS):
            day = local.date() + timedelta(days=offset)
            if not self._is_trading_day(day):
                continue
            candidate = datetime.combine(day, self._OPEN, tzinfo=self._tz)
            if candidate >= dt:
                return candidate.astimezone(UTC)
        raise RuntimeError(f"no {self.name} open found within {_SCAN_DAYS} days of {dt}")

    def next_close(self, dt: datetime) -> datetime | None:
        _require_aware(dt)
        local = dt.astimezone(self._tz)
        for offset in range(_SCAN_DAYS):
            day = local.date() + timedelta(days=offset)
            if not self._is_trading_day(day):
                continue
            candidate = datetime.combine(day, self._close_time(day), tzinfo=self._tz)
            if candidate > dt:
                return candidate.astimezone(UTC)
        raise RuntimeError(f"no {self.name} close found within {_SCAN_DAYS} days of {dt}")

    def session_day(self, dt: datetime) -> date:
        # ET calendar date: NYSE sessions never cross local midnight, so the daily-loss
        # window rolls between sessions, never inside one.
        _require_aware(dt)
        return dt.astimezone(self._tz).date()


_REGISTRY: dict[str, MarketCalendar] = {
    "always_open": AlwaysOpenCalendar(),
    "nyse": NYSECalendar(),
}


def get_calendar(name: str) -> MarketCalendar:
    """Resolve a calendar by name. Raises ``ValueError`` on unknown names so money-path
    callers fail safe instead of trading on a venue with no session definition."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown market calendar '{name}' (known: {sorted(_REGISTRY)})") from None
