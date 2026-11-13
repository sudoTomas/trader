"""Exit-ladder scorer: one channel call simulated bar by bar as the Phase 2 Bot would
trade it, at 1x, after the replay cost model (#139).

``simulate`` is pure: no I/O, no logging, the same answer for the same input. The caller
fetches the bars (``get_bars_between``) and decides what to store.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from trader.calls.parse import CallLevels, CallUpdate
from trader.core.symbols import AssetClass
from trader.core.types import Bar
from trader.eval.costs import ReplayCosts

#: Entry is the open of the next bar of this size after the post.
ENTRY_BAR = timedelta(minutes=15)
#: Cost notional per unit of position fraction; returns divide it back out.
_COST_NOTIONAL = 1000.0
_EPS = 1e-12


@dataclass(frozen=True)
class Fill:
    ts: datetime
    price: float
    fraction: float  # of the whole position
    reason: str  # entry | tp1..tp3 | stop | breakeven | closed | mark

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "price": self.price,
            "fraction": self.fraction,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CallScore:
    status: Literal["closed", "open", "unscorable"]
    reason: str | None
    entry: float | None
    stated_entry: float | None
    slippage_pct: float | None  # (entry - stated) / stated, in percent
    stop_used: float | None  # the starting stop; updates and break-even move it later
    fills: tuple[Fill, ...]
    gross_return: float  # fractions of the entry notional
    cost_return: float
    net_return: float
    first_touch: Literal["tp1", "stop", "neither"]
    hours_held: float | None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["fills"] = [f.as_dict() for f in self.fills]
        return out


def _ceil_to(ts: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=ts.tzinfo)
    steps = math.ceil((ts - epoch) / step)
    return epoch + steps * step


def _unscorable(levels: CallLevels, reason: str) -> CallScore:
    return CallScore(
        status="unscorable",
        reason=reason,
        entry=None,
        stated_entry=levels.entry,
        slippage_pct=None,
        stop_used=None,
        fills=(),
        gross_return=0.0,
        cost_return=0.0,
        net_return=0.0,
        first_touch="neither",
        hours_held=None,
    )


def simulate(
    levels: CallLevels,
    posted_at: datetime,
    updates: list[tuple[datetime, CallUpdate]],
    bars: list[Bar],
    *,
    stop_frac: float,
    costs: ReplayCosts,
    asset_class: AssetClass,
) -> CallScore:
    """Score one call against its bars.

    1. Entry bar: the first bar whose start is at or after ``posted_at`` rounded up to
       the next 15-minute boundary (a post at exactly 10:15 uses the 10:15 bar); entry =
       its open. No such bar: ``unscorable``, ``"no bars after post"``.
    2. Stop: long ``max(call stop, entry * (1 - stop_frac))``, short ``min(call stop,
       entry * (1 + stop_frac))``; no call stop, or one not on the loss side of the
       actual entry, uses the fraction alone.
    3. TPs: the stated TPs on the profitable side of the actual entry, in stated order,
       labelled tp1.. in that kept order. The position splits equally across them (the
       last one takes whatever is left). When stated TPs are dropped because the actual
       entry is past them, the next kept TP is labelled and scored as tp1. None left: hold
       until the stop, a ``closed`` update or the end of the bars.
    4. Walk bars from the entry bar. In each bar, updates first, then the stop (low <=
       stop for a long), then the TPs in order (high >= TP). A stop fill is at the stop,
       or at the open when the bar gapped through it; a TP fill is at the TP. After the
       first TP fill the stop goes to break-even (entry) unless already tighter, from the
       next bar. A stop exit filled at exactly the entry is reason ``breakeven``; one
       that gapped through it is ``stop``. The walk ends once the position is fully
       exited.
    5. Updates take effect at the first bar starting at or after their time:
       ``stop_to_breakeven`` -> stop = entry; ``stop_moved`` with ``new_stop`` -> that
       stop if tighter; ``closed`` -> exit the rest at that bar's open. ``tp_hit``,
       ``partial_profit`` and ``other`` change nothing (the Bot follows its own TPs).
    6. Bars run out with a position left: status ``open``, a ``mark`` fill at the last
       close.
    7. Returns are fractions of the entry notional: gross from the exit fills; costs are
       ``costs.fill(asset_class, notional=1000 * fraction)`` per fill (entry, every exit,
       the mark too), summed and divided by 1000; net = gross - costs. ``first_touch``:
       whichever of tp1 or the starting stop the bars touched first (the stop on a tie),
       over the bars walked.
    """
    series = sorted(bars, key=lambda b: b.ts)
    start = _ceil_to(posted_at, ENTRY_BAR)
    first = next((i for i, b in enumerate(series) if b.ts >= start), None)
    if first is None:
        return _unscorable(levels, "no bars after post")

    long = levels.side == "long"
    sign = 1.0 if long else -1.0
    entry_bar = series[first]
    entry = entry_bar.open

    def tighter(a: float, b: float) -> float:
        return max(a, b) if long else min(a, b)

    stop = entry * (1 - stop_frac) if long else entry * (1 + stop_frac)
    # A call stop on the wrong side of the actual entry is dropped, like the TPs.
    if levels.stop is not None and (levels.stop < entry if long else levels.stop > entry):
        stop = tighter(levels.stop, stop)
    original_stop = stop
    tps = [tp for tp in levels.tps if (tp > entry if long else tp < entry)]
    tp_share = 1.0 / len(tps) if tps else 0.0

    fills: list[Fill] = [Fill(entry_bar.ts, entry, 1.0, "entry")]
    remaining = 1.0
    next_tp = 0
    first_touch: Literal["tp1", "stop", "neither"] = "neither"
    pending = sorted(updates, key=lambda u: u[0])
    u = 0

    def exit_(bar: Bar, price: float, fraction: float, reason: str) -> None:
        nonlocal remaining
        fills.append(Fill(bar.ts, price, fraction, reason))
        remaining -= fraction

    for bar in series[first:]:
        # 5. Updates known by this bar's start.
        while u < len(pending) and pending[u][0] <= bar.ts:
            event = pending[u][1]
            u += 1
            if event.event == "stop_to_breakeven":
                stop = tighter(stop, entry)
            elif event.event == "stop_moved" and event.new_stop is not None:
                stop = tighter(stop, event.new_stop)
            elif event.event == "closed":
                exit_(bar, bar.open, remaining, "closed")
                break
        if remaining <= _EPS:
            break

        stop_hit = bar.low <= stop if long else bar.high >= stop
        if first_touch == "neither":
            if bar.low <= original_stop if long else bar.high >= original_stop:
                first_touch = "stop"
            elif tps and (bar.high >= tps[0] if long else bar.low <= tps[0]):
                first_touch = "tp1"

        # 4. Stop first.
        if stop_hit:
            price = min(bar.open, stop) if long else max(bar.open, stop)
            exit_(bar, price, remaining, "breakeven" if price == entry else "stop")
            break

        filled_tp = False
        while next_tp < len(tps) and (
            bar.high >= tps[next_tp] if long else bar.low <= tps[next_tp]
        ):
            last = next_tp == len(tps) - 1
            exit_(bar, tps[next_tp], remaining if last else tp_share, f"tp{next_tp + 1}")
            next_tp += 1
            filled_tp = True
        if remaining <= _EPS:
            break
        if filled_tp:
            stop = tighter(stop, entry)

    status: Literal["closed", "open"] = "closed"
    if remaining > _EPS:
        last_bar = series[-1]
        fills.append(Fill(last_bar.ts, last_bar.close, remaining, "mark"))
        status = "open"

    gross = sum(sign * f.fraction * (f.price - entry) / entry for f in fills[1:])
    cost = (
        sum(costs.fill(asset_class, notional=_COST_NOTIONAL * f.fraction).total for f in fills)
        / _COST_NOTIONAL
    )
    stated = levels.entry
    return CallScore(
        status=status,
        reason=None,
        entry=entry,
        stated_entry=stated,
        slippage_pct=(entry - stated) / stated * 100 if stated else None,
        stop_used=original_stop,
        fills=tuple(fills),
        gross_return=gross,
        cost_return=cost,
        net_return=gross - cost,
        first_touch=first_touch,
        hours_held=(fills[-1].ts - entry_bar.ts).total_seconds() / 3600,
    )


def size_usd(levels: CallLevels, sleeve_usd: float) -> float:
    """Dollars a call puts at risk: sleeve x allocation (1% when omitted) x leverage (1)."""
    return sleeve_usd * (levels.allocation_pct or 1.0) / 100 * (levels.leverage or 1.0)
