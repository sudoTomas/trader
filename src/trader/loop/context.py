"""ContextBuilder — everything the decider sees, assembled before the LLM is called.

**No LLM tool calls in this hot path.** v1 bound 28 MCP tools into the decide call and
got 120 s timeouts; here the context is pre-fetched, indicators are computed locally
(v1 fetched them over MCP and got null for 987/987 decisions), and the prompt is a
finished string by the time the model runs (see docs/lessons.md#indicators-over-mcp-returned-null).

Fetch-on-tick: bars come from the provider and are used in memory. Nothing is stored.
The Macro View slice occupies its own budgeted lane in the prompt: advisory by
construction — it feeds the decider only, never RiskGuard (see
docs/lessons.md#macro-view-advisory-firewall). The live call reads
``current()``. Replay passes ``research_as_of`` so the lane is the row that existed
at that instant, not whichever row is current today.

FRED prints (VIX by default) are fetched on use — the latest observation on or
before ``now``, not a stored window. RiskGuard does not read them either.

The exit-review lane (#34) is a held-position block — entry, age, unrealized P&L, the
latest sentiment, and an explicit ask to justify staying in — rendered only when
``exit_review_enabled`` is on AND a position is held. Dark by default; a decide-behavior
change that earns its flip through replay (docs/runbooks/decide-change-gate.md). It is
advisory like every other lane: RiskGuard never sees it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import structlog

from trader.core.symbols import Symbol
from trader.core.types import Bar
from trader.data.base import MarketData, MarketDataError
from trader.data.indicators import ema, realized_vol, rsi, sma
from trader.loop.scheduler import Trigger

log = structlog.get_logger(__name__)

BAR_LOOKBACK = 100
DEFAULT_INTERVALS = ("1h",)
MACRO_BUDGET_CHARS = 2000
BRIEF_BUDGET_CHARS = 1200
HELD_POSITION_BUDGET_CHARS = 700
# The explicit ask. The block exists to make the decider answer this, so it is one whole
# entry: under the lane's budget it stays or goes intact, never truncated mid-sentence.
EXIT_REVIEW_ASK = (
    "State whether the original thesis still holds. If it does not, or if the position "
    "has moved against you beyond your stated tolerance, prefer `close` (full, or partial "
    "via a smaller `notional_usd` to reduce) over `hold`; a hold must justify staying in."
)
# One daily print is enough for the decide prompt. VIX is the series the data
# layer was built for; more ids can be passed in, but the live builder uses this.
DEFAULT_FRED_SERIES = ("VIXCLS",)
FRED_LOOKBACK = 10


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
    and entries drop WHOLE — a truncated thesis reads as a different thesis. Only the
    global thesis and this symbol's asset-class section qualify; empty → no lane at all.
    """
    candidates: list[str] = []
    thesis = (view.get("global_thesis") or "").strip()
    if thesis:
        candidates.append(f"Global thesis: {thesis}")
    section = ((view.get("sections") or {}).get(asset_class) or "").strip()
    if section:
        candidates.append(f"{asset_class}: {section}")

    entries: list[str] = []
    used = 0
    for entry in candidates:
        if used + len(entry) > budget_chars:
            log.info("macro_entry_dropped_whole", version=view["version"], entry_chars=len(entry))
            continue
        entries.append(entry)
        used += len(entry)
    return MacroSlice(version=view["version"], entries=entries) if entries else None


@dataclass(frozen=True)
class BriefSlice:
    """The budgeted Candidate Brief for one symbol's prompt lane (advisory only)."""

    version: int
    entries: list[str]


def build_brief_slice(
    row: dict[str, Any], *, budget_chars: int = BRIEF_BUDGET_CHARS
) -> BriefSlice | None:
    """Render a brief into whole entries under its own character budget.

    Same discipline as the Macro lane: entries drop WHOLE (a truncated thesis reads as a
    different thesis), and an empty render means no lane at all.
    """
    candidates: list[str] = []
    thesis = (row.get("thesis") or "").strip()
    if thesis:
        candidates.append(f"Thesis: {thesis}")
    candidates += [p.strip() for p in row.get("points") or [] if p.strip()]

    entries: list[str] = []
    used = 0
    for entry in candidates:
        if used + len(entry) > budget_chars:
            log.info("brief_entry_dropped_whole", version=row["version"], entry_chars=len(entry))
            continue
        entries.append(entry)
        used += len(entry)
    return BriefSlice(version=row["version"], entries=entries) if entries else None


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


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    score = _as_float(sentiment.get("score")) if sentiment is not None else None
    if score is not None:
        volume = sentiment.get("volume") if sentiment is not None else None
        ts = sentiment.get("ts") if sentiment is not None else None
        line = f"Latest X sentiment: {score:+.2f}"
        if volume:
            line += f" ({volume})"
        if isinstance(ts, datetime):
            line += f" at {ts.isoformat()}"
        candidates.append(line)

    candidates.append(EXIT_REVIEW_ASK)

    entries: list[str] = []
    used = 0
    for candidate in candidates:
        if used + len(candidate) > budget_chars:
            log.info("held_position_entry_dropped_whole", entry_chars=len(candidate))
            continue
        entries.append(candidate)
        used += len(candidate)
    return HeldPositionSlice(entries=entries) if entries else None


def _latest_sentiment_at(rows: list[dict[str, Any]], as_of: datetime) -> dict[str, Any] | None:
    """Newest scored poll at or before ``as_of`` (id breaks ties)."""
    eligible = [row for row in rows if row.get("status", "ok") == "ok" and row["ts"] <= as_of]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row["ts"], int(row.get("id") or 0)))


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


@dataclass(frozen=True)
class Indicators:
    rsi_14: float | None = None
    sma_20: float | None = None
    ema_12: float | None = None
    realized_vol_20: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "rsi_14": self.rsi_14,
            "sma_20": self.sma_20,
            "ema_12": self.ema_12,
            "realized_vol_20": self.realized_vol_20,
        }


def compute_indicators(bars: list[Bar]) -> Indicators:
    """Local indicators, each independently optional: a short history yields Nones for
    the ones it cannot support rather than failing the whole wakeup."""

    def safe(fn, *args):
        try:
            return fn(*args)
        except ValueError:
            return None

    return Indicators(
        rsi_14=safe(rsi, bars, 14),
        sma_20=safe(sma, bars, 20),
        ema_12=safe(ema, bars, 12),
        realized_vol_20=safe(realized_vol, bars, 20),
    )


@dataclass(frozen=True)
class FredPrint:
    """Latest FRED observation on or before the context clock. Advisory facts only."""

    series_id: str
    obs_date: date
    value: float


@dataclass(frozen=True)
class DecisionContext:
    symbol: Symbol
    now: datetime
    triggers: list[Trigger]
    price: float
    bars: dict[str, list[Bar]]  # interval -> ascending bars
    indicators: dict[str, Indicators]  # interval -> indicators
    position_qty: float = 0.0
    equity: float = 0.0
    recent_decisions: list[dict[str, Any]] = field(default_factory=list)
    macro_view: MacroSlice | None = None  # advisory lane; RiskGuard never sees it
    brief: BriefSlice | None = None  # advisory lane; RiskGuard never sees it
    fred_series: tuple[FredPrint, ...] = ()  # advisory facts; RiskGuard never sees them
    analysts: list[str] = field(default_factory=list)  # desk lanes; advisory
    # Exit-review lane (#34): present only with the flag on AND a held position.
    held_position: HeldPositionSlice | None = None  # advisory; RiskGuard never sees it

    @property
    def primary_vol(self) -> float | None:
        """Realized vol for the primary interval — the guard's Kelly scaling input."""
        primary = next(iter(self.indicators.values()), None)
        return primary.realized_vol_20 if primary else None

    def to_prompt(self) -> str:
        """The full user-side prompt. Deterministic given the context (replay-ready)."""
        lines = [
            f"Symbol: {self.symbol.ticker} ({self.symbol.asset_class})",
            f"Time: {self.now.isoformat()}",
            f"Last price: {self.price:,.6g}",
            f"Open position: {self.position_qty:g} units",
            f"Account equity: {self.equity:,.2f} USD",
            "",
            "Wake triggers:",
        ]
        lines += [f"  - {t.type}: {t.detail}" for t in self.triggers] or ["  - (none)"]

        for interval, indicators in self.indicators.items():
            bars = self.bars.get(interval, [])
            lines += ["", f"Interval {interval} ({len(bars)} bars):"]
            lines += [f"  {name}: {value}" for name, value in indicators.as_dict().items()]
            if bars:
                closes = ", ".join(f"{b.close:,.6g}" for b in bars[-10:])
                lines.append(f"  last 10 closes: {closes}")

        if self.fred_series:
            lines += [
                "",
                "FRED macro series (latest print on or before this time; facts only,"
                " not a forecast — a deterministic risk layer owns sizing):",
            ]
            lines += [
                f"  {point.series_id}: {point.value:,.6g} ({point.obs_date.isoformat()})"
                for point in self.fred_series
            ]

        if self.macro_view:
            lines += [
                "",
                "Operator Macro View (advisory — informs direction and conviction only;"
                " a deterministic risk layer owns sizing)"
                f" [via research v{self.macro_view.version}]:",
            ]
            lines += [f"  {entry}" for entry in self.macro_view.entries]

        if self.brief:
            lines += [
                "",
                "Candidate Brief (advisory — informs direction and conviction only;"
                " a deterministic risk layer owns sizing)"
                f" [via brief v{self.brief.version}]:",
            ]
            lines += [f"  {entry}" for entry in self.brief.entries]

        if self.analysts:
            lines += [
                "",
                "Analyst desk (advisory reads — contradictions between analysts are"
                " information, not noise; a deterministic risk layer owns sizing):",
            ]
            lines += [f"  {entry}" for entry in self.analysts]

        if self.held_position:
            lines += [
                "",
                "Held position review (advisory — the thesis question is yours;"
                " a deterministic risk layer owns sizing):",
            ]
            lines += [f"  {entry}" for entry in self.held_position.entries]

        if self.recent_decisions:
            lines += ["", "Recent decisions (newest first):"]
            lines += [
                f"  - {d['ts']} {d['trigger']} -> {d['status']}"
                f" {(d.get('decision') or {}).get('action', '')}"
                for d in self.recent_decisions
            ]
        return "\n".join(lines)


class ContextBuilder:
    """Assembles a DecisionContext. All I/O happens here, none in the decider."""

    def __init__(
        self,
        market_data: MarketData,
        *,
        intervals: tuple[str, ...] = DEFAULT_INTERVALS,
        lookback: int = BAR_LOOKBACK,
        macro_views: Any | None = None,  # store.repos.MacroViewsRepo; absent = no lane
        macro_budget_chars: int = MACRO_BUDGET_CHARS,
        briefs: Any | None = None,  # store.repos.BriefsRepo; absent = no lane (dark)
        brief_budget_chars: int = BRIEF_BUDGET_CHARS,
        fred: Any | None = None,  # data.fred.Fred; absent → provider.fred, else no lane
        fred_series: tuple[str, ...] = DEFAULT_FRED_SERIES,
        exit_review_enabled: bool = False,  # #34 lane default; build() may override per call
        sentiment: Any | None = None,  # store.repos.SentimentRepo; read only for the lane
        held_position_budget_chars: int = HELD_POSITION_BUDGET_CHARS,
    ) -> None:
        self._data = market_data
        self._intervals = intervals
        self._lookback = lookback
        self._macro_views = macro_views
        self._macro_budget = macro_budget_chars
        self._briefs = briefs
        self._brief_budget = brief_budget_chars
        self._fred = fred
        self._fred_series = fred_series
        self._exit_review = exit_review_enabled
        self._sentiment = sentiment
        self._held_budget = held_position_budget_chars
        # (latest version, rows). Invalidated when a newer version appears, so a
        # long-lived replay harness does not freeze the first run's history.
        self._macro_cache: tuple[int, list[dict[str, Any]]] | None = None
        self._brief_cache: dict[str, tuple[int, list[dict[str, Any]]]] = {}

    async def _macro_history(self) -> list[dict[str, Any]]:
        repo = self._macro_views
        latest = await repo.current()
        if latest is None:
            return []
        version = int(latest["version"])
        cached = self._macro_cache
        if cached is not None and cached[0] == version:
            return cached[1]
        rows = await _history_from(repo, latest, symbol=None)
        self._macro_cache = (version, rows)
        return rows

    async def _brief_history(self, symbol: str) -> list[dict[str, Any]]:
        repo = self._briefs
        latest = await repo.current(symbol)
        if latest is None:
            return []
        version = int(latest["version"])
        cached = self._brief_cache.get(symbol)
        if cached is not None and cached[0] == version:
            return cached[1]
        rows = await _history_from(repo, latest, symbol=symbol)
        self._brief_cache[symbol] = (version, rows)
        return rows

    def _fred_client(self) -> Any | None:
        # Explicit client wins. Otherwise the composite the live runner already
        # holds (market_data.fred) — the runner itself does not take a fred argument.
        if self._fred is not None:
            return self._fred
        return getattr(self._data, "fred", None)

    async def _load_fred(self, as_of: datetime) -> tuple[FredPrint, ...]:
        fred = self._fred_client()
        if fred is None or not self._fred_series:
            return ()
        end = as_of.date()
        prints: list[FredPrint] = []
        for series_id in self._fred_series:
            try:
                rows = await fred.get_series(series_id, FRED_LOOKBACK, observation_end=end)
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
        return tuple(prints)

    async def build(
        self,
        symbol: Symbol,
        *,
        now: datetime,
        triggers: list[Trigger],
        position_qty: float = 0.0,
        equity: float = 0.0,
        recent_decisions: list[dict[str, Any]] | None = None,
        research_as_of: datetime | None = None,
        include_briefs: bool = True,
        position: Mapping[str, Any] | None = None,  # positions row (or a replay seed)
        exit_review: bool | None = None,  # None → the builder's default
    ) -> DecisionContext:
        bars: dict[str, list[Bar]] = {}
        indicators: dict[str, Indicators] = {}
        for interval in self._intervals:
            try:
                fetched = await self._data.get_bars(symbol, interval, self._lookback)
            except MarketDataError as exc:
                log.warning(
                    "context_bars_unavailable",
                    symbol=symbol.ticker,
                    interval=interval,
                    error=str(exc),
                )
                fetched = []
            bars[interval] = fetched
            indicators[interval] = compute_indicators(fetched)

        # Quote-first mark: the last completed bar is up to an interval stale (more for
        # sip bars behind their delayed end); bars are the fallback, not the source.
        price = 0.0
        try:
            price = (await self._data.get_latest_quote(symbol)).mid
        except MarketDataError as exc:
            primary = bars.get(self._intervals[0], [])
            if primary:
                price = primary[-1].close
            else:
                log.warning("context_price_unavailable", symbol=symbol.ticker, error=str(exc))

        macro_view: MacroSlice | None = None
        if self._macro_views is not None:
            try:
                # Live reads current(). Replay passes research_as_of so a step cannot
                # see a view compiled after it (created_at <= step ts).
                if research_as_of is None:
                    current = await self._macro_views.current()
                else:
                    current = _latest_at(await self._macro_history(), research_as_of, "created_at")
                if current is not None:
                    macro_view = build_macro_slice(
                        current, symbol.asset_class, budget_chars=self._macro_budget
                    )
            except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
                log.warning("macro_view_unavailable", error=str(exc))

        brief: BriefSlice | None = None
        if include_briefs and self._briefs is not None:
            try:
                if research_as_of is None:
                    current_brief = await self._briefs.current(symbol.ticker)
                else:
                    current_brief = _latest_at(
                        await self._brief_history(symbol.ticker), research_as_of, "ts"
                    )
                if current_brief is not None:
                    brief = build_brief_slice(current_brief, budget_chars=self._brief_budget)
            except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
                log.warning("brief_unavailable", error=str(exc))

        try:
            fred_series = await self._load_fred(now)
        except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
            log.warning("fred_unavailable", error=str(exc))
            fred_series = ()

        held_position: HeldPositionSlice | None = None
        review = self._exit_review if exit_review is None else exit_review
        if review and position_qty != 0 and position is not None and price > 0:
            try:
                sentiment_row = await self._held_sentiment(symbol.ticker, research_as_of)
                held_position = build_held_position_slice(
                    position,
                    price=price,
                    now=now,
                    sentiment=sentiment_row,
                    budget_chars=self._held_budget,
                )
            except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
                log.warning("held_position_unavailable", symbol=symbol.ticker, error=str(exc))

        return DecisionContext(
            symbol=symbol,
            now=now,
            triggers=triggers,
            price=price,
            bars=bars,
            indicators=indicators,
            position_qty=position_qty,
            equity=equity,
            recent_decisions=recent_decisions or [],
            macro_view=macro_view,
            brief=brief,
            fred_series=fred_series,
            held_position=held_position,
        )

    async def _held_sentiment(self, ticker: str, as_of: datetime | None) -> dict[str, Any] | None:
        """Latest scored poll for the held symbol. Live reads ``latest()``; replay pins
        it to the step so a later poll cannot leak into a historical prompt."""
        if self._sentiment is None:
            return None
        if as_of is None:
            return await self._sentiment.latest(ticker)
        return _latest_sentiment_at(await self._sentiment.recent(ticker, limit=500), as_of)
