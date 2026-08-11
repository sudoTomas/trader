"""Indicator unit tests — known values, pure, no I/O."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from trader.core.types import Bar
from trader.data.indicators import ema, realized_vol, rsi, sma

T0 = datetime(2026, 8, 11, tzinfo=UTC)


def bars_from(closes: list[float]) -> list[Bar]:
    return [
        Bar("X", "1m", T0 + timedelta(minutes=i), c, c, c, c, 1.0) for i, c in enumerate(closes)
    ]


def test_sma_known_value():
    assert sma(bars_from([1, 2, 3, 4, 5]), 3) == pytest.approx(4.0)


def test_ema_known_value():
    # Seed = mean(1,2,3) = 2; alpha = 0.5: 4 -> 3.0, 5 -> 4.0.
    assert ema(bars_from([1, 2, 3, 4, 5]), 3) == pytest.approx(4.0)


def test_rsi_known_values():
    # Monotonic rise → no losses → RSI 100.
    assert rsi(bars_from(list(range(1, 20))), 14) == 100.0
    # Hand-computed Wilder smoothing, period 3 over [1,2,3,2,3]:
    # avg_gain 2/3 → 7/9, avg_loss 1/3 → 2/9, RS 3.5 → RSI 77.78.
    assert rsi(bars_from([1, 2, 3, 2, 3]), 3) == pytest.approx(77.7778, abs=1e-3)


def test_realized_vol_known_values():
    assert realized_vol(bars_from([5] * 10), 5) == 0.0
    # Alternating 1,2 closes: log returns ±ln2, sample std = 2*ln2/sqrt(3).
    vol = realized_vol(bars_from([1, 2, 1, 2, 1]), 4)
    assert vol == pytest.approx(2 * math.log(2) / math.sqrt(3))
    annualized = realized_vol(bars_from([1, 2, 1, 2, 1]), 4, periods_per_year=252)
    assert annualized == pytest.approx(vol * math.sqrt(252))


def test_indicators_fail_loud_on_insufficient_data():
    short = bars_from([1, 2])
    for fn, args in ((sma, (5,)), (ema, (5,)), (rsi, (14,)), (realized_vol, (20,))):
        with pytest.raises(ValueError, match="needs >="):
            fn(short, *args)
