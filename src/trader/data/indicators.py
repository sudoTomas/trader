"""Indicators, computed in-process from fetched bars. Pure numpy, no I/O.

Never fetch these over a network/MCP hop: v1 pulled RSI/SMA over the macro-lab MCP and
got null for 987/987 decisions (docs/architecture.md). Insufficient data raises — the
money path must fail loud, never trade on a silent NaN.
"""

from __future__ import annotations

import math

import numpy as np

from trader.core.types import Bar


def _closes(bars: list[Bar], minimum: int, indicator: str) -> np.ndarray:
    if len(bars) < minimum:
        raise ValueError(f"{indicator} needs >= {minimum} bars, got {len(bars)}")
    return np.array([b.close for b in bars], dtype=np.float64)


def sma(bars: list[Bar], period: int) -> float:
    """Simple moving average of the last ``period`` closes."""
    closes = _closes(bars, period, f"sma({period})")
    return float(closes[-period:].mean())


def ema(bars: list[Bar], period: int) -> float:
    """Exponential moving average (seeded with the SMA of the first ``period`` closes)."""
    closes = _closes(bars, period, f"ema({period})")
    alpha = 2.0 / (period + 1)
    value = closes[:period].mean()
    for close in closes[period:]:
        value = alpha * close + (1 - alpha) * value
    return float(value)


def rsi(bars: list[Bar], period: int = 14) -> float:
    """Relative Strength Index with Wilder's smoothing, in [0, 100]."""
    closes = _closes(bars, period + 1, f"rsi({period})")
    deltas = np.diff(closes)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))


def realized_vol(
    bars: list[Bar], period: int = 20, *, periods_per_year: float | None = None
) -> float:
    """Std dev of log returns over the last ``period`` bars.

    Per-bar by default; pass ``periods_per_year`` (e.g. 365*24*60 for 1m crypto bars,
    252 for daily equities) to annualize by sqrt.
    """
    closes = _closes(bars, period + 1, f"realized_vol({period})")
    returns = np.diff(np.log(closes[-(period + 1) :]))
    vol = float(returns.std(ddof=1))
    if periods_per_year is not None:
        vol *= math.sqrt(periods_per_year)
    return vol
