"""Screener (M5, #5): a static universe → a bounded, ranked shortlist. Pure, no I/O.

Bars come in (one ascending daily list per symbol, fetched by the caller on demand — see
docs/lessons.md#no-market-data-storage; nothing here is persisted or cached) and a
deterministic ranking comes out. No LLM anywhere: the score is arithmetic the operator
can check by hand, and the ``reason`` string is the arithmetic spelled out.

Ranking: risk-adjusted momentum — the mean of the 20d and 60d returns divided by the
20d annualized realized vol. Dollar volume is a liquidity *filter*, not a score input.
Symbols already watched by the decide loop are excluded (the screener proposes new
names; it never re-ranks the book). Ties break on the ticker, so equal inputs give
equal output regardless of dict order.

The screener is a query on all three surfaces; it is NOT wired into the decide loop.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass

from trader.core.types import Bar
from trader.data.indicators import realized_vol

#: Trading days per year for annualizing the daily realized vol.
TRADING_DAYS_PER_YEAR = 252
MOMENTUM_SHORT = 20
MOMENTUM_LONG = 60
VOL_PERIOD = 20
DOLLAR_VOLUME_PERIOD = 20
#: Bars needed to compute every metric: the long momentum needs MOMENTUM_LONG + 1 closes.
MIN_BARS = MOMENTUM_LONG + 1
#: Vol floor for the ratio, so a flat synthetic series cannot divide by zero.
_VOL_FLOOR = 1e-6
MAX_LIMIT = 50


@dataclass(frozen=True)
class Candidate:
    """One ranked symbol with the metrics that ranked it and a plain-language reason."""

    symbol: str
    close: float
    momentum_20d: float  # fractional return over the last 20 bars
    momentum_60d: float  # fractional return over the last 60 bars
    realized_vol_20d: float  # annualized std dev of daily log returns, last 20 bars
    dollar_volume_20d: float  # mean close * volume over the last 20 bars, USD
    score: float  # mean(momentum_20d, momentum_60d) / realized_vol_20d
    reason: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ScreenResult:
    """The bounded shortlist plus what was considered, excluded, and skipped (and why)."""

    universe: str
    considered: int
    eligible: int
    limit: int
    min_dollar_volume: float
    excluded: list[str]  # loop symbols dropped before ranking, sorted
    skipped: dict[str, str]  # symbol -> why it could not be ranked, sorted by symbol
    shortlist: list[Candidate]

    def as_dict(self) -> dict[str, object]:
        return {
            "universe": self.universe,
            "considered": self.considered,
            "eligible": self.eligible,
            "limit": self.limit,
            "min_dollar_volume": self.min_dollar_volume,
            "excluded": list(self.excluded),
            "skipped": dict(self.skipped),
            "shortlist": [c.as_dict() for c in self.shortlist],
        }


def _momentum(bars: list[Bar], period: int) -> float:
    return bars[-1].close / bars[-1 - period].close - 1.0


def _dollar_volume(bars: list[Bar], period: int) -> float:
    window = bars[-period:]
    return sum(b.close * b.volume for b in window) / len(window)


def score_symbol(symbol: str, bars: list[Bar]) -> Candidate:
    """Metrics + score for one symbol. Raises ``ValueError`` on too few bars."""
    if len(bars) < MIN_BARS:
        raise ValueError(f"{symbol}: needs >= {MIN_BARS} daily bars, got {len(bars)}")
    mom_short = _momentum(bars, MOMENTUM_SHORT)
    mom_long = _momentum(bars, MOMENTUM_LONG)
    vol = realized_vol(bars, VOL_PERIOD, periods_per_year=TRADING_DAYS_PER_YEAR)
    dollar_volume = _dollar_volume(bars, DOLLAR_VOLUME_PERIOD)
    blended = (mom_short + mom_long) / 2.0
    score = blended / max(vol, _VOL_FLOOR)
    reason = (
        f"{MOMENTUM_SHORT}d {mom_short:+.1%}, {MOMENTUM_LONG}d {mom_long:+.1%}, "
        f"vol {vol:.1%} ann., ${dollar_volume / 1e6:,.0f}M/day; "
        f"risk-adjusted momentum {score:+.2f}"
    )
    return Candidate(
        symbol=symbol,
        close=float(bars[-1].close),
        momentum_20d=float(mom_short),
        momentum_60d=float(mom_long),
        realized_vol_20d=float(vol),
        dollar_volume_20d=float(dollar_volume),
        score=float(score),
        reason=reason,
    )


def rank_universe(
    universe: str,
    bars_by_symbol: Mapping[str, list[Bar]],
    *,
    exclude: Iterable[str],
    min_dollar_volume: float,
    limit: int,
) -> ScreenResult:
    """Rank ``bars_by_symbol`` into at most ``limit`` candidates.

    ``exclude`` (the loop's symbols) are dropped before ranking; symbols below
    ``min_dollar_volume`` or with fewer than ``MIN_BARS`` bars are skipped with a reason.
    Deterministic: the same bars give the same result in any input order.
    """
    bound = max(1, min(int(limit), MAX_LIMIT))
    excluded_set = {s for s in exclude}
    excluded: list[str] = []
    skipped: dict[str, str] = {}
    candidates: list[Candidate] = []
    for symbol in sorted(bars_by_symbol):
        if symbol in excluded_set:
            excluded.append(symbol)
            continue
        bars = bars_by_symbol[symbol]
        if len(bars) < MIN_BARS:
            skipped[symbol] = f"insufficient bars ({len(bars)} < {MIN_BARS})"
            continue
        candidate = score_symbol(symbol, bars)
        if candidate.dollar_volume_20d < min_dollar_volume:
            skipped[symbol] = (
                f"dollar volume ${candidate.dollar_volume_20d / 1e6:,.1f}M/day below "
                f"${min_dollar_volume / 1e6:,.1f}M"
            )
            continue
        candidates.append(candidate)
    candidates.sort(key=lambda c: (-c.score, c.symbol))
    return ScreenResult(
        universe=universe,
        considered=len(bars_by_symbol),
        eligible=len(candidates),
        limit=bound,
        min_dollar_volume=float(min_dollar_volume),
        excluded=excluded,
        skipped=skipped,
        shortlist=candidates[:bound],
    )


__all__ = ["MIN_BARS", "Candidate", "ScreenResult", "rank_universe", "score_symbol"]
