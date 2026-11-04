"""The Scorecard (#143) — each desk seat graded on settled outcomes, at the 5-day horizon.

A seat's view of a decision is a signed conviction in [-1, 1]:

- ``decide``: the Draft as the model wrote it (``raw_output``, before any critic): an
  open buy or a short's close is ``+conviction``, an open sell or a long's close
  ``-conviction``, a hold 0.
- ``analyst:<lane>``: bullish ``+confidence``, bearish ``-confidence``, neutral 0.
- ``critic``: only on a Draft it reviewed, as a call on the Draft's direction: approve
  ``+1``, shrink ``+0.5``, veto ``-1``, times the Draft's direction. A veto of a Draft
  that would have lost is a hit. Pass-throughs are not views.

Three numbers per seat:

- **hit rate**: of the directional views (non-zero), the share whose sign matched the
  excess return's (vs the benchmark, so a long in a rising market is not free).
- **slugging**: the mean |excess| of the hits over the mean |excess| of the misses.
- **rank IC**: the Spearman correlation of the signed conviction with the forward
  return, over every view (ties share their average rank). Pooled over the settled
  decisions, not per date: a Bot views a handful of symbols at a time, too few to rank.

A number with too little behind it (no hits or no misses, fewer than three views, no
spread to rank) is None, never a guess.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from trader.learning.settlement import HORIZON_DAYS, direction

DECIDE = "decide"
CRITIC = "critic"
MIN_IC_VIEWS = 3
CRITIC_WEIGHTS = {"approve": 1.0, "shrink": 0.5, "veto": -1.0}


def _draft(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The decide model's Draft as it wrote it; the journaled decision if unreadable."""
    try:
        raw = json.loads(row.get("raw_output") or "")
    except (TypeError, ValueError):
        raw = None
    if isinstance(raw, dict) and raw.get("action") in ("open", "close", "hold"):
        return {
            "action": raw["action"],
            "intent": {"side": raw.get("side")},
            "conviction": raw.get("conviction"),
        }
    return row.get("decision")


def _conviction(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 1.0) if math.isfinite(number) else 0.0


def seat_views(row: Mapping[str, Any]) -> dict[str, float]:
    """Every seat's signed view of one settled decision."""
    views: dict[str, float] = {}
    draft = _draft(row)
    draft_view = direction(draft)
    if draft is not None:
        views[DECIDE] = draft_view * _conviction(draft.get("conviction"))
    desk = row.get("desk_verdicts") or {}
    for read in desk.get("analysts") or []:
        if not read.get("ok"):
            continue
        sign = {"bullish": 1, "bearish": -1}.get(read.get("stance"), 0)
        views[f"analyst:{read['lane']}"] = sign * _conviction(read.get("confidence"))
    verdict = (desk.get("critic") or {}).get("verdict")
    if verdict in CRITIC_WEIGHTS and draft_view != 0:
        views[CRITIC] = CRITIC_WEIGHTS[verdict] * draft_view
    return views


def _ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks; a tie group shares the mean of the positions it spans."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Pearson correlation of the average ranks; None under three points or no spread."""
    if len(x) < MIN_IC_VIEWS:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    dx = [r - mx for r in rx]
    dy = [r - my for r in ry]
    denominator = math.sqrt(sum(d * d for d in dx) * sum(d * d for d in dy))
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy, strict=True)) / denominator


def score(views: Iterable[tuple[float, float, float]]) -> dict[str, Any]:
    """One seat's numbers from ``(signed conviction, forward return, excess return)``."""
    views = list(views)
    directional = [(v, excess) for v, _, excess in views if v != 0]
    hits = [abs(e) for v, e in directional if v * e > 0]
    misses = [abs(e) for v, e in directional if v * e <= 0]
    slugging = None
    if hits and misses and sum(misses) > 0:
        slugging = (sum(hits) / len(hits)) / (sum(misses) / len(misses))
    return {
        "views": len(views),
        "directional": len(directional),
        "hits": len(hits),
        "hit_rate": len(hits) / len(directional) if directional else None,
        "slugging": slugging,
        "rank_ic": spearman([v for v, _, _ in views], [f for _, f, _ in views]),
    }


def scorecard(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The Scorecard over settled decisions (``SettlementsRepo.scored`` rows)."""
    by_seat: dict[str, list[tuple[float, float, float]]] = {}
    settled = 0
    for row in rows:
        settled += 1
        forward, excess = float(row["forward_return"]), float(row["excess_return"])
        for seat, view in seat_views(row).items():
            by_seat.setdefault(seat, []).append((view, forward, excess))
    return {
        "horizon_days": HORIZON_DAYS,
        "settled": settled,
        "seats": {seat: score(views) for seat, views in sorted(by_seat.items())},
    }
