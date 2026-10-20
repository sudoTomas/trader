"""Sources (#124) — the information a Bot reads when it decides.

A Source reads what a producer already wrote (the research compiler's Macro View and
briefs, the sentiment poller's scores, FRED on use, the positions row) and renders one
prompt lane from it. It never fetches into storage: the live path stores no market data.
Producers are unchanged; a Source only reads.

``read(symbol, as_of=None, params)`` is the one contract. ``as_of=None`` reads the
current state; a timestamp pins the read to the state that existed at that instant, so
replay sees what the live loop saw and never a row written later. Every slice carries a
**Source version** — the immutable row id (``macro_views.version``, ``briefs.version``,
``sentiment_scores.id``), ``series@obs_date`` for FRED, or a content hash for the held
position — and the instant it was read as of. The journal records one per Source used
(``decisions.source_versions``).

A Bot subscribes to Sources by name in its spec (``sources: [{source, params}]``).
``params`` holds the subscription's own settings (FRED's series, a lane budget) plus what
the ContextBuilder knows only at decide time: ``now`` (the context clock), ``price`` and
``position``.

Every lane is advisory: it feeds the decider only. RiskGuard never imports this module
(asserted by test; see docs/lessons.md#macro-view-advisory-firewall).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import structlog

from trader.core.symbols import Symbol

log = structlog.get_logger(__name__)

MACRO_VIEW = "macro_view"
BRIEF = "brief"
FRED = "fred"
SENTIMENT = "sentiment"
HELD_POSITION = "held_position"
#: The sentiment subscription's one setting: also poll the Bot's symbols it does not hold.
POLL_UNHELD = "poll_unheld"

MACRO_BUDGET_CHARS = 2000
BRIEF_BUDGET_CHARS = 1200
HELD_POSITION_BUDGET_CHARS = 900
# The explicit ask. The block exists to make the decider answer this, so it is one whole
# entry: under the lane's budget it stays or goes intact, never truncated mid-sentence.
# No numbers here on purpose: replay run 18 (docs/reports/2026-09-29-exit-review-replay-gate.md)
# showed a stated "1%" becomes a mechanical stop the model applies without judgment. Deterministic
# stops live in the loop's stop-loss (#74) and RiskGuard, never in prompt text.
EXIT_REVIEW_ASK = (
    "State whether the original thesis still holds. If it does not, or if the position "
    "has moved against you beyond your stated tolerance, prefer `close` (full, or partial "
    "via a smaller `notional_usd` to reduce) over `hold`; a hold must justify staying in."
)
# One daily print is enough for the decide prompt. VIX is the series the data
# layer was built for; a subscription can name more.
DEFAULT_FRED_SERIES = ("VIXCLS",)
FRED_LOOKBACK = 10
# How far back the pinned sentiment read looks for the newest poll before as_of.
SENTIMENT_HISTORY = 500


# --- the contract -----------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSlice:
    """One Source's read for one prompt: what it said, and exactly which state it was.

    ``detail`` is the lane's structured render (a MacroSlice, the FRED prints, ...) for
    the prompt and the desk; it is not part of the slice's identity.
    """

    source: str
    version: int | str
    as_of: datetime
    text: str
    detail: Any = field(default=None, compare=False, repr=False)

    def stamp(self) -> dict[str, Any]:
        """The Source version as the journal records it."""
        return {"version": self.version, "as_of": self.as_of.isoformat()}


class Source(Protocol):
    name: str

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None: ...


@dataclass(frozen=True)
class Subscription:
    """A Bot's subscription to one Source, as its spec stores it."""

    source: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def as_spec(self) -> dict[str, Any]:
        return {"source": self.source, "params": dict(self.params)}


def subscriptions(spec: Iterable[Subscription | Mapping[str, Any]]) -> tuple[Subscription, ...]:
    """Normalise a spec's ``sources`` list (or Subscriptions) into Subscriptions."""
    return tuple(
        s if isinstance(s, Subscription) else Subscription(s["source"], s.get("params") or {})
        for s in spec
    )


def bot_one_subscriptions(
    *, macro_view: bool = True, briefs: bool = False, exit_review: bool = False
) -> list[dict[str, Any]]:
    """The Sources the pre-Bots flags turned on: FRED and the Macro View always, the brief
    with ``TRADER_BRIEFS_ENABLED``, the held position with ``TRADER_EXIT_REVIEW_ENABLED``.
    Replay's ``macro_view: false`` is the one way to drop the Macro View."""
    subs = [Subscription(FRED, {"series": list(DEFAULT_FRED_SERIES)})]
    if macro_view:
        subs.append(Subscription(MACRO_VIEW))
    if briefs:
        subs.append(Subscription(BRIEF))
    if exit_review:
        subs.append(Subscription(HELD_POSITION))
    return [s.as_spec() for s in subs]


def polls_unheld(spec: Iterable[Subscription | Mapping[str, Any]]) -> bool:
    """Whether the Bot's sentiment subscription asks the poller to score its symbols it
    does not hold too (``params.poll_unheld``). Off by default: sentiment polling is the
    biggest LLM cost, so only held symbols are scored (see
    docs/lessons.md#sentiment-held-symbols-only)."""
    return any(
        s.source == SENTIMENT and s.params.get(POLL_UNHELD) is True for s in subscriptions(spec)
    )


def build_sources(db: Any, market_data: Any, *, fred: Any | None = None) -> dict[str, Source]:
    """Every Source, over the store and the market-data provider. Construction does no
    I/O; a subscription decides which ones a decide actually reads."""
    from trader.store.repos import BriefsRepo, MacroViewsRepo, SentimentRepo

    sentiment = SentimentSource(SentimentRepo(db))
    return {
        MACRO_VIEW: MacroViewSource(MacroViewsRepo(db)),
        BRIEF: BriefSource(BriefsRepo(db)),
        # The composite provider holds the FRED client beside the bar chains.
        FRED: FredSource(fred if fred is not None else getattr(market_data, "fred", None)),
        SENTIMENT: sentiment,
        HELD_POSITION: HeldPositionSource(sentiment),
    }


def _read_clock(as_of: datetime | None, params: Mapping[str, Any]) -> datetime:
    """The instant a read is stamped with: the pin, else the context clock."""
    if as_of is not None:
        return as_of
    return params.get("now") or datetime.now(tz=UTC)


def _whole_entries(candidates: list[str], budget_chars: int, event: str, **kw: Any) -> list[str]:
    """Entries drop WHOLE under a lane's budget — a truncated thesis reads as a different
    thesis."""
    entries: list[str] = []
    used = 0
    for entry in candidates:
        if used + len(entry) > budget_chars:
            log.info(event, entry_chars=len(entry), **kw)
            continue
        entries.append(entry)
        used += len(entry)
    return entries


# --- as-of reads over version chains ----------------------------------------------------


def _latest_at(rows: list[dict[str, Any]], as_of: datetime, ts_key: str) -> dict[str, Any] | None:
    """Newest row whose timestamp is at or before ``as_of`` (version breaks ties)."""
    eligible = [row for row in rows if row[ts_key] <= as_of]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row[ts_key], int(row["version"])))


async def _history_from(
    repo: Any, start: dict[str, Any], *, symbol: str | None
) -> list[dict[str, Any]]:
    """Walk ``get(version)`` from ``start`` down to version 1.

    Repos expose ``current()`` / ``get()`` only — no as-of query. Versions are a single
    identity, so the chain is the history. ``symbol`` keeps other symbols' briefs out.
    """
    rows: list[dict[str, Any]] = []
    row: dict[str, Any] | None = start
    version = int(start["version"])
    while version >= 1:
        if row is not None and (symbol is None or row.get("symbol") == symbol):
            rows.append(row)
        version -= 1
        if version < 1:
            break
        row = await repo.get(version)
    return rows


class _VersionChain:
    """A repo's version history, cached until a newer version appears so a long-lived
    replay harness does not freeze the first run's history."""

    def __init__(self) -> None:
        self._cache: dict[str | None, tuple[int, list[dict[str, Any]]]] = {}

    async def rows(
        self, repo: Any, latest: dict[str, Any] | None, *, symbol: str | None
    ) -> list[dict[str, Any]]:
        if latest is None:
            return []
        version = int(latest["version"])
        cached = self._cache.get(symbol)
        if cached is not None and cached[0] == version:
            return cached[1]
        rows = await _history_from(repo, latest, symbol=symbol)
        self._cache[symbol] = (version, rows)
        return rows


# --- Macro View -------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroSlice:
    """The budgeted Macro View slice for one symbol's prompt lane (advisory only)."""

    version: int
    entries: list[str]


def build_macro_slice(
    view: dict[str, Any], asset_class: str, *, budget_chars: int = MACRO_BUDGET_CHARS
) -> MacroSlice | None:
    """Render the view into whole entries under a character budget.

    The lane has its own budget (operator judgment and market facts starve independently)
    and entries drop WHOLE. Only the global thesis and this symbol's asset-class section
    qualify; empty → no lane at all.
    """
    candidates: list[str] = []
    thesis = (view.get("global_thesis") or "").strip()
    if thesis:
        candidates.append(f"Global thesis: {thesis}")
    section = ((view.get("sections") or {}).get(asset_class) or "").strip()
    if section:
        candidates.append(f"{asset_class}: {section}")
    entries = _whole_entries(
        candidates, budget_chars, "macro_entry_dropped_whole", version=view["version"]
    )
    return MacroSlice(version=view["version"], entries=entries) if entries else None


class MacroViewSource:
    """The one shared Macro View (``macro_views``). Version: ``macro_views.version``."""

    name = MACRO_VIEW

    def __init__(self, repo: Any) -> None:  # store.repos.MacroViewsRepo
        self._repo = repo
        self._chain = _VersionChain()

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None:
        latest = await self._repo.current()
        if as_of is None:
            row = latest
        else:
            # A pinned read cannot see a view compiled after it (created_at <= as_of).
            history = await self._chain.rows(self._repo, latest, symbol=None)
            row = _latest_at(history, as_of, "created_at")
        if row is None:
            return None
        macro = build_macro_slice(
            row, symbol.asset_class, budget_chars=params.get("budget_chars", MACRO_BUDGET_CHARS)
        )
        if macro is None:
            return None
        return SourceSlice(
            self.name, macro.version, _read_clock(as_of, params), "\n".join(macro.entries), macro
        )


# --- Candidate Brief --------------------------------------------------------------------


@dataclass(frozen=True)
class BriefSlice:
    """The budgeted Candidate Brief for one symbol's prompt lane (advisory only)."""

    version: int
    entries: list[str]


def build_brief_slice(
    row: dict[str, Any], *, budget_chars: int = BRIEF_BUDGET_CHARS
) -> BriefSlice | None:
    """Render a brief into whole entries under its own character budget.

    Same discipline as the Macro lane: entries drop WHOLE, and an empty render means no
    lane at all.
    """
    candidates: list[str] = []
    thesis = (row.get("thesis") or "").strip()
    if thesis:
        candidates.append(f"Thesis: {thesis}")
    candidates += [p.strip() for p in row.get("points") or [] if p.strip()]
    entries = _whole_entries(
        candidates, budget_chars, "brief_entry_dropped_whole", version=row["version"]
    )
    return BriefSlice(version=row["version"], entries=entries) if entries else None


class BriefSource:
    """The symbol's Candidate Brief (``briefs``). Version: ``briefs.version``."""

    name = BRIEF

    def __init__(self, repo: Any) -> None:  # store.repos.BriefsRepo
        self._repo = repo
        self._chain = _VersionChain()

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None:
        latest = await self._repo.current(symbol.ticker)
        if as_of is None:
            row = latest
        else:
            history = await self._chain.rows(self._repo, latest, symbol=symbol.ticker)
            row = _latest_at(history, as_of, "ts")
        if row is None:
            return None
        brief = build_brief_slice(row, budget_chars=params.get("budget_chars", BRIEF_BUDGET_CHARS))
        if brief is None:
            return None
        return SourceSlice(
            self.name, brief.version, _read_clock(as_of, params), "\n".join(brief.entries), brief
        )


# --- X sentiment ------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_sentiment_at(rows: list[dict[str, Any]], as_of: datetime) -> dict[str, Any] | None:
    """Newest scored poll at or before ``as_of`` (id breaks ties)."""
    eligible = [row for row in rows if row.get("status", "ok") == "ok" and row["ts"] <= as_of]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row["ts"], int(row.get("id") or 0)))


def sentiment_line(row: Mapping[str, Any] | None) -> str | None:
    """One scored poll as a prompt line; None when the row carries no score."""
    score = _as_float(row.get("score")) if row is not None else None
    if row is None or score is None:
        return None
    line = f"Latest X sentiment: {score:+.2f}"
    if row.get("volume"):
        line += f" ({row['volume']})"
    if isinstance(row.get("ts"), datetime):
        line += f" at {row['ts'].isoformat()}"
    return line


class SentimentSource:
    """The latest scored X sentiment poll (``sentiment_scores``). Version: its row id."""

    name = SENTIMENT

    def __init__(self, repo: Any) -> None:  # store.repos.SentimentRepo
        self._repo = repo

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None:
        if as_of is None:
            row = await self._repo.latest(symbol.ticker)
        else:
            # Pinned so a later poll cannot leak into a historical prompt.
            rows = await self._repo.recent(symbol.ticker, limit=SENTIMENT_HISTORY)
            row = _latest_sentiment_at(rows, as_of)
        line = sentiment_line(row)
        if row is None or line is None:
            return None
        return SourceSlice(self.name, row.get("id"), _read_clock(as_of, params), line, row)


# --- FRED -------------------------------------------------------------------------------


@dataclass(frozen=True)
class FredPrint:
    """Latest FRED observation on or before the context clock. Advisory facts only."""

    series_id: str
    obs_date: date
    value: float


class FredSource:
    """FRED prints, fetched on use — the latest observation on or before the clock, not a
    stored window. Version: ``series@obs_date`` per series, comma-joined."""

    name = FRED

    def __init__(self, client: Any | None) -> None:  # data.fred.Fred; None = no lane
        self._client = client

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None:
        if self._client is None:
            return None
        clock = _read_clock(as_of, params)
        end = clock.date()
        prints: list[FredPrint] = []
        for series_id in params.get("series") or DEFAULT_FRED_SERIES:
            try:
                rows = await self._client.get_series(series_id, FRED_LOOKBACK, observation_end=end)
            except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
                log.warning("fred_series_unavailable", series_id=series_id, error=str(exc))
                continue
            # Defense in depth: a client that ignores observation_end must not leak
            # a later print into a replay step. Keep only the latest point, not the window.
            eligible = [(obs_date, value) for obs_date, value in rows if obs_date <= end]
            if not eligible:
                continue
            obs_date, value = eligible[-1]
            prints.append(FredPrint(series_id=series_id, obs_date=obs_date, value=value))
        if not prints:
            return None
        version = ",".join(f"{p.series_id}@{p.obs_date.isoformat()}" for p in prints)
        text = "\n".join(
            f"{p.series_id}: {p.value:,.6g} ({p.obs_date.isoformat()})" for p in prints
        )
        return SourceSlice(self.name, version, clock, text, tuple(prints))


# --- held position (exit review, #34) ---------------------------------------------------


def unrealized_pnl_frac(price: float, qty: float, avg_entry: float | None) -> float | None:
    """Signed move vs entry. Longs gain when price rises; shorts gain when it falls.

    Shared by the runner's marks (the position-risk trigger) and the held-position block,
    so the prompt and the trigger never disagree about the same position.
    """
    if price <= 0 or qty == 0 or avg_entry is None or avg_entry <= 0:
        return None
    move = (price - avg_entry) / avg_entry
    return move if qty > 0 else -move


def _format_age(delta: timedelta) -> str:
    total = max(int(delta.total_seconds()), 0)
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


@dataclass(frozen=True)
class HeldPositionSlice:
    """The budgeted held-position block for the exit-review lane (advisory only)."""

    entries: list[str]


def build_held_position_slice(
    position: Mapping[str, Any],
    *,
    price: float,
    now: datetime,
    sentiment: Mapping[str, Any] | None,
    budget_chars: int = HELD_POSITION_BUDGET_CHARS,
) -> HeldPositionSlice | None:
    """Render a held position into whole entries under the lane's own budget.

    ``position`` is a positions-row-shaped mapping: ``qty``, ``avg_entry_price``, and
    either ``opened_at`` (exact; the replay seed) or ``updated_at`` (the positions row,
    which reconcile and fills bump — so it bounds the age from below and is labeled that
    way). No entry price means nothing to review against: no block. Entries drop WHOLE.
    """
    qty = _as_float(position.get("qty")) or 0.0
    entry = _as_float(position.get("avg_entry_price"))
    if qty == 0.0 or entry is None or entry <= 0:
        return None
    side = "long" if qty > 0 else "short"
    candidates = [f"Entry price: {entry:,.6g} USD ({side} {abs(qty):g} units)"]

    opened_at = position.get("opened_at")
    updated_at = position.get("updated_at")
    if isinstance(opened_at, datetime):
        candidates.append(
            f"Position age: {_format_age(now - opened_at)} (opened {opened_at.isoformat()})"
        )
    elif isinstance(updated_at, datetime):
        candidates.append(
            f"Position age: at least {_format_age(now - updated_at)}"
            f" (position row last changed {updated_at.isoformat()})"
        )

    pnl = unrealized_pnl_frac(price, qty, entry)
    if pnl is not None:
        candidates.append(f"Unrealized P&L: {pnl:+.2%} vs entry (mark {price:,.6g})")

    line = sentiment_line(sentiment)
    if line is not None:
        candidates.append(line)

    candidates.append(EXIT_REVIEW_ASK)
    entries = _whole_entries(candidates, budget_chars, "held_position_entry_dropped_whole")
    return HeldPositionSlice(entries=entries) if entries else None


class HeldPositionSource:
    """The held-position block: entry, age, unrealized P&L, the latest sentiment and the
    exit-review ask. Reads the ``position`` and ``price`` the builder passes (a positions
    row live, the sim's seed in replay) and the sentiment pinned to ``as_of``.
    Version: a hash of the rendered block."""

    name = HELD_POSITION

    def __init__(self, sentiment: SentimentSource | None) -> None:
        self._sentiment = sentiment

    async def read(
        self, symbol: Symbol, *, as_of: datetime | None, params: Mapping[str, Any]
    ) -> SourceSlice | None:
        position = params.get("position")
        price = params.get("price") or 0.0
        if position is None or price <= 0:
            return None
        clock = _read_clock(as_of, params)
        sentiment = (
            await self._sentiment.read(symbol, as_of=as_of, params=params)
            if self._sentiment is not None
            else None
        )
        held = build_held_position_slice(
            position,
            price=price,
            now=params.get("now") or clock,
            sentiment=sentiment.detail if sentiment is not None else None,
            budget_chars=params.get("budget_chars", HELD_POSITION_BUDGET_CHARS),
        )
        if held is None:
            return None
        text = "\n".join(held.entries)
        version = hashlib.sha256(text.encode()).hexdigest()[:16]
        return SourceSlice(self.name, version, clock, text, held)
