"""Core domain unit tests — pure, no network, no DB."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.core.calendars import AlwaysOpenCalendar, NYSECalendar, get_calendar
from trader.core.symbols import AssetClass, default_registry, make_symbol
from trader.core.types import Action, Bar, Decision, OrderIntent, Side

ET = ZoneInfo("America/New_York")


def et(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# --- calendars: always_open -------------------------------------------------


def test_always_open_is_always_open():
    cal = AlwaysOpenCalendar()
    saturday_night = datetime(2026, 8, 15, 3, 0, tzinfo=UTC)
    assert cal.is_open(saturday_night)
    assert cal.next_open(saturday_night) == saturday_night
    assert cal.next_close(saturday_night) is None
    assert cal.session_day(saturday_night) == date(2026, 8, 15)


def test_calendars_reject_naive_datetimes():
    naive = datetime(2026, 8, 12, 12, 0)
    for cal in (AlwaysOpenCalendar(), NYSECalendar()):
        with pytest.raises(ValueError, match="timezone-aware"):
            cal.is_open(naive)


# --- calendars: nyse --------------------------------------------------------


def test_nyse_regular_session_boundaries():
    cal = NYSECalendar()
    # Wed 2026-08-12 is a plain trading day. Open inclusive, close exclusive.
    assert not cal.is_open(et(2026, 8, 12, 9, 29))
    assert cal.is_open(et(2026, 8, 12, 9, 30))
    assert cal.is_open(et(2026, 8, 12, 15, 59))
    assert not cal.is_open(et(2026, 8, 12, 16, 0))


def test_nyse_closed_weekends_and_holidays():
    cal = NYSECalendar()
    assert not cal.is_open(et(2026, 8, 15, 12, 0))  # Saturday
    assert not cal.is_open(et(2026, 8, 16, 12, 0))  # Sunday
    assert not cal.is_open(et(2026, 7, 3, 12, 0))  # Independence Day observed (Friday)
    assert not cal.is_open(et(2026, 11, 26, 12, 0))  # Thanksgiving


def test_nyse_half_day_closes_at_1300():
    cal = NYSECalendar()
    assert cal.is_open(et(2026, 11, 27, 12, 59))  # day after Thanksgiving
    assert not cal.is_open(et(2026, 11, 27, 13, 0))


def test_nyse_next_open_skips_weekend_and_holiday():
    cal = NYSECalendar()
    # Saturday → Monday 09:30 ET.
    assert cal.next_open(et(2026, 8, 15, 12, 0)) == et(2026, 8, 17, 9, 30).astimezone(UTC)
    # Thu 2026-07-02 after close → Fri Jul 3 is the observed holiday → Mon Jul 6.
    assert cal.next_open(et(2026, 7, 2, 17, 0)) == et(2026, 7, 6, 9, 30).astimezone(UTC)
    # Already open → unchanged.
    open_now = et(2026, 8, 12, 11, 0)
    assert cal.next_open(open_now) == open_now


def test_nyse_next_close_and_session_day():
    cal = NYSECalendar()
    assert cal.next_close(et(2026, 8, 12, 11, 0)) == et(2026, 8, 12, 16, 0).astimezone(UTC)
    # Closed Saturday → Monday's close; a UTC evening instant still buckets to the ET date.
    assert cal.next_close(et(2026, 8, 15, 12, 0)) == et(2026, 8, 17, 16, 0).astimezone(UTC)
    late_utc = datetime(2026, 8, 13, 1, 30, tzinfo=UTC)  # 21:30 ET on Aug 12
    assert cal.session_day(late_utc) == date(2026, 8, 12)


def test_nyse_dst_shifts_utc_open():
    cal = NYSECalendar()
    # Winter (EST, UTC-5): 14:30 UTC is the open. Summer (EDT, UTC-4): 13:30 UTC.
    assert cal.is_open(datetime(2026, 1, 7, 14, 30, tzinfo=UTC))
    assert not cal.is_open(datetime(2026, 1, 7, 14, 29, tzinfo=UTC))
    assert cal.is_open(datetime(2026, 8, 12, 13, 30, tzinfo=UTC))
    assert not cal.is_open(datetime(2026, 8, 12, 13, 29, tzinfo=UTC))


def test_get_calendar_unknown_fails_safe():
    with pytest.raises(ValueError, match="unknown market calendar"):
        get_calendar("nasdaq")


# --- symbols ----------------------------------------------------------------


def test_default_registry_crypto_slice():
    reg = default_registry()
    btc = reg.get("BTC/USD")
    assert btc.asset_class is AssetClass.CRYPTO
    assert btc.fractionable and not btc.shortable
    assert btc.market_calendar.name == "always_open"
    assert btc.for_provider("alpaca_trading") == "BTCUSD"
    assert btc.for_provider("unknown_provider") == "BTC/USD"  # canonical fallback
    assert {s.ticker for s in reg.by_class(AssetClass.CRYPTO)} == {"BTC/USD", "ETH/USD"}


def test_unknown_symbol_fails_safe():
    with pytest.raises(KeyError, match="unknown symbol 'DOGE/USD'"):
        default_registry().get("DOGE/USD")


def test_class_defaults_and_overrides():
    spy = make_symbol("SPY", AssetClass.US_EQUITY)
    assert spy.shortable and spy.calendar == "nyse"
    gld = make_symbol("GLD", AssetClass.COMMODITY_ETF)
    assert not gld.shortable and gld.calendar == "nyse"
    weird = make_symbol("XYZ", AssetClass.US_EQUITY, shortable=False, min_notional=5.0)
    assert not weird.shortable and weird.min_notional == 5.0


def test_registry_extension_is_immutable():
    # QQQ: an equity NOT in the seed universe (SPY is the only seeded equity).
    reg = default_registry()
    extended = reg.with_symbol(make_symbol("QQQ", AssetClass.US_EQUITY))
    assert extended.get("QQQ").ticker == "QQQ"
    with pytest.raises(KeyError):
        reg.get("QQQ")  # original untouched


# --- types ------------------------------------------------------------------


def test_types_are_frozen_and_validated():
    bar = Bar("BTC/USD", "1m", datetime(2026, 8, 12, tzinfo=UTC), 1, 2, 0.5, 1.5, 10)
    with pytest.raises(FrozenInstanceError):
        bar.close = 99  # type: ignore[misc]
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar("BTC/USD", "1m", datetime(2026, 8, 12), 1, 2, 0.5, 1.5, 10)


def test_order_intent_requires_exactly_one_size():
    OrderIntent("BTC/USD", Side.BUY, notional=100.0)
    with pytest.raises(ValueError, match="exactly one"):
        OrderIntent("BTC/USD", Side.BUY)
    with pytest.raises(ValueError, match="exactly one"):
        OrderIntent("BTC/USD", Side.BUY, qty=1.0, notional=100.0)
    with pytest.raises(ValueError, match="positive"):
        OrderIntent("BTC/USD", Side.SELL, qty=-1.0)


def test_decision_intent_matches_action():
    Decision("BTC/USD", Action.HOLD, 0.4, "nothing to do")
    intent = OrderIntent("BTC/USD", Side.BUY, notional=50.0)
    Decision("BTC/USD", Action.OPEN, 0.8, "momentum", intent=intent)
    with pytest.raises(ValueError, match="intent is required"):
        Decision("BTC/USD", Action.OPEN, 0.8, "momentum")
    with pytest.raises(ValueError, match="intent is required"):
        Decision("BTC/USD", Action.HOLD, 0.4, "hold", intent=intent)
    with pytest.raises(ValueError, match="conviction"):
        Decision("BTC/USD", Action.HOLD, 1.5, "overconfident")
