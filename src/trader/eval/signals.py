"""Shadow-signal scoring (#144): did the journaled readings say anything about the bars
that came after them?

Every decide that subscribes to a shadow signal journals its reading in
``decisions.signals``. This scores those readings against the return ``horizon`` 1h
bars later, fetched now from the market-data provider and used in memory (nothing is
stored):

- **Kronos**: band coverage (the share of realized returns inside the forecast's
  p10..p90, which should be near 80%) and directional hit rate (more than half the
  paths up, and the price did go up; or the reverse), per symbol and overall.
- **Crypto derivatives**: rank IC (Spearman) of the funding rate, the open-interest
  change and the basis against the forward return, pooled and per symbol.

Several Bots and wakeups can read the same signal for the same bar; each bar counts
once. Forward windows overlap from one hour to the next, so ``n`` overstates how many
independent observations there are; the report reads it that way.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from trader.core.symbols import SymbolRegistry
from trader.core.types import Bar
from trader.data.kronos import KRONOS_INTERVAL
from trader.loop.sources import CRYPTO_DERIVATIVES, KRONOS

BAR = timedelta(hours=1)
MAX_LOOKBACK = 2000
KRONOS_BAND = 0.8
DERIVATIVE_FACTORS = ("funding_rate", "oi_change_24h", "basis")


def _ts(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def forward_return(
    bars: list[Bar], base_ts: datetime, horizon: int, *, base: float | None = None
) -> float | None:
    """The return from the bar at or before ``base_ts`` to the close ``horizon`` bars
    later; ``base`` overrides the start price. None when that bar has not closed yet."""
    stamps = [b.ts for b in bars]
    idx = bisect.bisect_right(stamps, base_ts) - 1
    if idx < 0 or idx + horizon >= len(bars):
        return None
    start = base if base is not None else bars[idx].close
    if not start:
        return None
    return bars[idx + horizon].close / start - 1.0


def rank_ic(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation (ties share their average rank). None under 3 pairs or
    when either side does not vary."""
    if len(xs) < 3:
        return None

    def ranks(values: list[float]) -> np.ndarray:
        arr = np.asarray(values, dtype=float)
        order = arr.argsort(kind="mergesort")
        out = np.empty(len(arr))
        out[order] = np.arange(len(arr), dtype=float)
        for value in np.unique(arr):  # average the ranks of ties
            tied = arr == value
            out[tied] = out[tied].mean()
        return out

    rx, ry = ranks(xs), ranks(ys)
    if rx.std() == 0 or ry.std() == 0:
        return None
    ic = float(np.corrcoef(rx, ry)[0, 1])
    return None if math.isnan(ic) else ic


def _kronos_stats(points: list[tuple[dict[str, Any], float]]) -> dict[str, Any]:
    if not points:
        return {"n": 0, "coverage": None, "hit_rate": None}
    inside = [r["p10"] <= realized <= r["p90"] for r, realized in points]
    calls = [
        (r["share_up"] > 0.5) == (realized > 0)
        for r, realized in points
        if r["share_up"] != 0.5 and realized != 0
    ]
    return {
        "n": len(points),
        "coverage": sum(inside) / len(inside),
        "hit_rate": sum(calls) / len(calls) if calls else None,
    }


def _derivative_stats(points: list[tuple[dict[str, Any], float]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(points)}
    for factor in DERIVATIVE_FACTORS:
        pairs = [(r[factor], realized) for r, realized in points if r.get(factor) is not None]
        out[factor] = {
            "n": len(pairs),
            "ic": rank_ic([x for x, _ in pairs], [y for _, y in pairs]),
        }
    return out


def score_readings(
    rows: Iterable[Mapping[str, Any]], bars: Mapping[str, list[Bar]], *, horizon: int
) -> dict[str, Any]:
    """Score journaled ``signals`` rows (``symbol``, ``signals``) against ``bars``
    (symbol -> ascending, closed 1h bars). Pure: no I/O."""
    kronos: dict[str, dict[Any, tuple[dict[str, Any], float]]] = {}
    derivs: dict[str, dict[Any, tuple[dict[str, Any], float]]] = {}
    pending = 0
    for row in rows:
        symbol = row["symbol"]
        series = bars.get(symbol) or []
        signals = row.get("signals") or {}
        forecast = (signals.get(KRONOS) or {}).get("reading")
        if forecast:
            base_ts = _ts(forecast["last_bar_ts"])
            realized = forward_return(series, base_ts, horizon, base=forecast["last_close"])
            if realized is None:
                pending += 1
            else:
                kronos.setdefault(symbol, {})[base_ts] = (forecast, realized)
        reading = (signals.get(CRYPTO_DERIVATIVES) or {}).get("reading")
        if reading:
            # The last bar that had closed when the reading was taken.
            base_ts = _ts(signals[CRYPTO_DERIVATIVES]["as_of"]) - BAR
            realized = forward_return(series, base_ts, horizon)
            if realized is None:
                pending += 1
            else:
                key = base_ts.replace(minute=0, second=0, microsecond=0)
                derivs.setdefault(symbol, {})[key] = (reading, realized)

    def flat(by_symbol: dict[str, dict[Any, tuple]]) -> list[tuple]:
        return [p for points in by_symbol.values() for p in points.values()]

    return {
        "horizon_bars": horizon,
        "pending": pending,
        KRONOS: {
            "band": KRONOS_BAND,
            "overall": _kronos_stats(flat(kronos)),
            "by_symbol": {s: _kronos_stats(list(p.values())) for s, p in sorted(kronos.items())},
        },
        CRYPTO_DERIVATIVES: {
            "pooled": _derivative_stats(flat(derivs)),
            "by_symbol": {
                s: _derivative_stats(list(p.values())) for s, p in sorted(derivs.items())
            },
        },
    }


async def score_signals(
    db: Any,
    market_data: Any,
    registry: SymbolRegistry,
    *,
    days: int,
    horizon: int,
    bot_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score the last ``days`` of journaled shadow-signal readings (one Bot's with
    ``bot_id``). Bars are fetched per symbol for this call only."""
    now = now or datetime.now(tz=UTC)
    since = now - timedelta(days=days)
    sql = "SELECT ts, symbol, signals FROM decisions WHERE signals IS NOT NULL AND ts >= %s"
    args: list[Any] = [since]
    if bot_id is not None:
        sql += " AND bot_id = %s"
        args.append(bot_id)
    rows = await db.fetch_all(sql + " ORDER BY ts", tuple(args))
    lookback = min(int((now - since) / BAR) + horizon + 2, MAX_LOOKBACK)
    bars: dict[str, list[Bar]] = {}
    unavailable: dict[str, str] = {}
    for ticker in sorted({r["symbol"] for r in rows if r["symbol"]}):
        try:
            fetched = await market_data.get_bars(registry.get(ticker), KRONOS_INTERVAL, lookback)
        except Exception as exc:  # noqa: BLE001 - one symbol's bars missing scores the rest
            unavailable[ticker] = str(exc)
            continue
        bars[ticker] = [b for b in fetched if b.ts + BAR <= now]  # closed bars only
    out = score_readings(rows, bars, horizon=horizon)
    return {
        "days": days,
        "since": since.isoformat(),
        "readings": len(rows),
        **out,
        "bars_unavailable": unavailable,
    }
