"""Vendor Sources (#142): SEC EDGAR filings, Polymarket odds, StockTwits/Reddit counts.

Each reads the rows its producer (``trader.research.edgar`` / ``polymarket`` / ``retail``)
wrote to ``vendor_reads``; none fetches. Version: the row id. Every one is opt-in per Bot
(a subscription by name) and every one is held to the point-in-time rule
(``trader.loop.pit``):

- **sec_filings** dates every item by when SEC accepted it, so a pinned read takes the
  newest row, even one fetched after ``as_of``, and serves only the filings and the
  as-filed facts accepted by then. A restated figure reads at the vintage that was
  current: an amendment counts from its own acceptance, never before.
- **polymarket** and **retail_sentiment** carry no historical vintage: a pinned read
  takes the newest row fetched at or before ``as_of``, so replay before the producer
  first ran sees no lane at all.

Retail sentiment is its own lane beside X sentiment, never merged into it, and it
always states its message counts so thin data reads as thin. Blind replay drops every
lane here (they name the asset or carry calendar dates).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from trader.core.symbols import Symbol
from trader.loop import pit
from trader.loop.sources import SourceSlice, _read_clock, _whole_entries

log = structlog.get_logger(__name__)

SEC_FILINGS = "sec_filings"
POLYMARKET = "polymarket"
RETAIL_SENTIMENT = "retail_sentiment"
VENDOR_SOURCES = (SEC_FILINGS, POLYMARKET, RETAIL_SENTIMENT)

#: Each vendor lane's prompt header (``DecisionContext.to_prompt``).
VENDOR_LANES = {
    SEC_FILINGS: (
        "SEC EDGAR (as filed by this time; facts only — a deterministic risk layer owns sizing)"
    ),
    POLYMARKET: (
        "Polymarket odds (crowd-priced probabilities, not forecasts; advisory — a"
        " deterministic risk layer owns sizing)"
    ),
    RETAIL_SENTIMENT: (
        "Retail chatter, StockTwits/Reddit (counts first: thin data is thin; advisory — a"
        " deterministic risk layer owns sizing)"
    ),
}

SEC_BUDGET_CHARS = 1200
POLYMARKET_BUDGET_CHARS = 900
RETAIL_BUDGET_CHARS = 500
#: Filings older than this at the read clock are not "recent".
SEC_LOOKBACK_DAYS = 90
SEC_MAX_FILINGS = 5
#: How far back a pinned row-level read looks for the newest row before as_of.
VENDOR_HISTORY = 200
#: Under this many messages a feed's read is flagged thin.
THIN_MESSAGES = 10
# A duration fact is a quarter at 60-115 days and a fiscal year at 300-400.
_QUARTER = (60, 115)
_YEAR = (300, 400)


def _ts(value: Any) -> datetime | None:
    """A stored ISO instant (or datetime) as an aware datetime; None when unreadable."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _amount(value: float, unit: str) -> str:
    """A filed figure, readable: billions and millions for USD, the raw value otherwise.
    Never a ``$``: the compiler would stamp it example-not-current."""
    if unit == "USD":
        for scale, suffix in ((1e9, "B"), (1e6, "M")):
            if abs(value) >= scale:
                return f"{value / scale:,.2f}{suffix} USD"
        return f"{value:,.0f} USD"
    return f"{value:,.4g} {unit}"


async def _row_at(repo: Any, source: str, symbol: str | None, as_of: datetime | None):
    """The newest row (live), or the newest fetched at or before ``as_of`` (pinned)."""
    if as_of is None:
        return await repo.latest(source, symbol)
    rows = await repo.recent(source, symbol, limit=VENDOR_HISTORY)
    return pit.latest_visible(
        rows, as_of, available_at=lambda r: r["ts"], order=lambda r: (r["ts"], int(r["id"]))
    )


# --- SEC EDGAR ----------------------------------------------------------------------------


def _span(fact: Mapping[str, Any]) -> int | None:
    if not fact.get("start"):
        return None
    return (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days


def _fact_line(item: str, facts: list[Mapping[str, Any]], as_of: datetime | None) -> tuple:
    """(line, available_at) for one line item's latest period, at the vintage current as of
    ``as_of``; (None, None) when nothing was filed by then."""
    seen = [f for f in facts if pit.visible(_ts(f.get("available_at")), as_of)]
    for low, high, label in ((*_QUARTER, "quarter"), (*_YEAR, "fiscal year"), (0, 0, "")):
        if label:
            pool = [f for f in seen if (s := _span(f)) is not None and low <= s <= high]
        else:
            pool = [f for f in seen if _span(f) is None]  # an instant: a balance
        if not pool:
            continue
        fact = max(pool, key=lambda f: (f["end"], _ts(f["available_at"])))
        period = f"{label} ended {fact['end']}" if label else f"at {fact['end']}"
        line = (
            f"{item}: {_amount(float(fact['val']), fact.get('unit', 'USD'))}"
            f" ({period}; {fact.get('form', '?')} filed {fact.get('filed', '?')})"
        )
        return line, _ts(fact["available_at"])
    return None, None


def render_sec(
    payload: Mapping[str, Any],
    *,
    as_of: datetime | None,
    clock: datetime,
    lookback_days: int = SEC_LOOKBACK_DAYS,
    budget_chars: int = SEC_BUDGET_CHARS,
) -> tuple[list[str], datetime | None]:
    """(entries, availability time of the newest one) for what was on file by ``as_of``."""
    since = clock - timedelta(days=lookback_days)
    filings = [
        f
        for f in payload.get("filings") or []
        if (at := _ts(f.get("accepted_at"))) is not None and pit.visible(at, as_of) and at >= since
    ]
    filings.sort(key=lambda f: _ts(f["accepted_at"]), reverse=True)  # type: ignore[arg-type, return-value]
    candidates: list[str] = []
    stamps: list[datetime] = []
    for f in filings[:SEC_MAX_FILINGS]:
        detail = f"items {f['items']}" if f.get("items") else None
        if not detail and f.get("report_date"):
            detail = f"period {f['report_date']}"
        candidates.append(
            f"Filing: {f['form']} filed {f['filed']}" + (f" ({detail})" if detail else "")
        )
        stamps.append(_ts(f["accepted_at"]))  # type: ignore[arg-type]
    by_item: dict[str, list[Mapping[str, Any]]] = {}
    for fact in payload.get("facts") or []:
        by_item.setdefault(fact["item"], []).append(fact)
    for item, facts in by_item.items():
        line, at = _fact_line(item, facts, as_of)
        if line is not None:
            candidates.append(f"As filed: {line}")
            stamps.append(at)
    entries = _whole_entries(candidates, budget_chars, "sec_entry_dropped_whole")
    kept = [s for c, s in zip(candidates, stamps, strict=True) if c in entries]
    return entries, max(kept) if kept else None


class SecFilingsSource:
    """Recent 8-K/10-Q/10-K filings and as-filed fundamentals for a US filer."""

    name = SEC_FILINGS

    def __init__(self, repo: Any) -> None:  # store.repos.VendorReadsRepo
        self._repo = repo

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None:
        # Every item is dated, so the newest row serves any as_of item by item.
        row = await self._repo.latest(self.name, symbol.ticker)
        if row is None:
            return None
        clock = _read_clock(as_of, params)
        entries, available_at = render_sec(
            row["payload"],
            as_of=as_of,
            clock=clock,
            lookback_days=params.get("lookback_days", SEC_LOOKBACK_DAYS),
            budget_chars=params.get("budget_chars", SEC_BUDGET_CHARS),
        )
        if not entries:
            return None
        return SourceSlice(
            self.name, row["id"], clock, "\n".join(entries), row, available_at=available_at
        )


# --- Polymarket ---------------------------------------------------------------------------


def _odds(probability: float) -> str:
    return f"{probability:.0%}" if 0.01 <= probability <= 0.99 else f"{probability:.1%}"


def render_polymarket(
    payload: Mapping[str, Any], *, budget_chars: int = POLYMARKET_BUDGET_CHARS
) -> list[str]:
    candidates: list[str] = []
    for topic in payload.get("topics") or []:
        for m in topic.get("markets") or []:
            line = f"{topic['topic']}: {m['question']} — {m['outcome']} {_odds(m['probability'])}"
            extras = [f"{_amount(float(m.get('volume_usd') or 0), 'USD')} traded"]
            if m.get("end_date"):
                extras.append(f"resolves {m['end_date']}")
            if m.get("one_week_change"):
                extras.append(f"1w {m['one_week_change'] * 100:+.1f}pp")
            candidates.append(f"{line} ({', '.join(extras)})")
    return _whole_entries(candidates, budget_chars, "polymarket_entry_dropped_whole")


class PolymarketSource:
    """Market-wide odds on macro events (Fed decisions, CPI, recession): the same lane for
    every symbol. Live-only data: replay sees a row only from its fetch on."""

    name = POLYMARKET

    def __init__(self, repo: Any) -> None:  # store.repos.VendorReadsRepo
        self._repo = repo

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None:
        row = await _row_at(self._repo, self.name, None, as_of)
        if row is None:
            return None
        entries = render_polymarket(
            row["payload"], budget_chars=params.get("budget_chars", POLYMARKET_BUDGET_CHARS)
        )
        if not entries:
            return None
        text = "\n".join([f"Fetched {row['ts'].isoformat()}", *entries])
        return SourceSlice(
            self.name, row["id"], _read_clock(as_of, params), text, row, available_at=row["ts"]
        )


# --- StockTwits / Reddit ------------------------------------------------------------------


@dataclass(frozen=True)
class _Feed:
    label: str
    noun: str
    window: str


_FEEDS = {
    "stocktwits": _Feed("StockTwits", "messages", "window_hours"),
    "reddit": _Feed("Reddit", "posts", "window_days"),
}


def _feed_line(key: str, feed: Mapping[str, Any]) -> str:
    spec = _FEEDS[key]
    if feed.get("status") != "ok":
        return f"{spec.label}: unavailable ({feed.get('reason') or 'no data'})"
    count = int(feed.get(spec.noun) or 0)
    unit = "h" if spec.window == "window_hours" else "d"
    shown = f"{count}+" if feed.get("capped") else str(count)
    line = f"{spec.label}: {shown} {spec.noun} in {feed.get(spec.window)}{unit}"
    notes: list[str] = []
    if key == "stocktwits":
        bull, bear = int(feed.get("bullish") or 0), int(feed.get("bearish") or 0)
        labels = f"{bull} bullish, {bear} bearish labels"
        if bull + bear:
            labels += f"; net {(bull - bear) / (bull + bear):+.2f}"
        notes.append(labels)
    if key == "reddit" and feed.get("subreddits"):
        notes.append(", ".join(f"r/{s}" for s in feed["subreddits"]))
    if count < THIN_MESSAGES and not feed.get("capped"):
        notes.append("thin")
    return f"{line} ({'; '.join(notes)})" if notes else line


def render_retail(payload: Mapping[str, Any]) -> list[str]:
    return [_feed_line(key, payload[key]) for key in _FEEDS if key in payload]


class RetailSentimentSource:
    """StockTwits message counts and labels plus Reddit post counts for the symbol.
    Live-only data: replay sees a row only from its fetch on."""

    name = RETAIL_SENTIMENT

    def __init__(self, repo: Any) -> None:  # store.repos.VendorReadsRepo
        self._repo = repo

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None:
        row = await _row_at(self._repo, self.name, symbol.ticker, as_of)
        if row is None:
            return None
        lines = render_retail(row["payload"])
        if not lines:
            return None
        text = "\n".join([f"Fetched {row['ts'].isoformat()}", *lines])
        if len(text) > params.get("budget_chars", RETAIL_BUDGET_CHARS):
            log.info("retail_lane_dropped_whole", chars=len(text))
            return None
        return SourceSlice(
            self.name, row["id"], _read_clock(as_of, params), text, row, available_at=row["ts"]
        )
