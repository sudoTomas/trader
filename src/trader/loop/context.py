"""ContextBuilder — everything the decider sees, assembled before the LLM is called.

**No LLM tool calls in this hot path.** v1 bound 28 MCP tools into the decide call and
got 120 s timeouts; here the context is pre-fetched, indicators are computed locally
(v1 fetched them over MCP and got null for 987/987 decisions), and the prompt is a
finished string by the time the model runs (see docs/lessons.md#indicators-over-mcp-returned-null).

Fetch-on-tick: bars come from the provider and are used in memory. Nothing is stored.

Every other lane is a **Source** (``trader.loop.sources``, #124): the Macro View, the
Candidate Brief, FRED prints, X sentiment and the held-position block (exit review, #34).
The builder reads exactly the Sources the Bot version subscribes to, each in its own
budgeted lane, and keeps every slice on the context so the journal can record its Source
version. The live call reads current state (``as_of=None``); replay passes ``as_of`` so
every lane is the state that existed at that instant, not whichever row is current today.
Every lane is advisory by construction — it feeds the decider only, never RiskGuard (see
docs/lessons.md#macro-view-advisory-firewall) — and a failing Source drops its lane; it
never blocks a decide.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from trader.core.symbols import Symbol
from trader.core.types import Bar
from trader.data.base import MarketData, MarketDataError
from trader.data.indicators import ema, realized_vol, rsi, sma
from trader.loop.scheduler import Trigger
from trader.loop.sources import (  # noqa: F401 - lane renderers re-exported for callers
    BRIEF,
    BRIEF_BUDGET_CHARS,
    EXIT_REVIEW_ASK,
    FRED,
    HELD_POSITION,
    HELD_POSITION_BUDGET_CHARS,
    MACRO_BUDGET_CHARS,
    MACRO_VIEW,
    SENTIMENT,
    BriefSlice,
    FredPrint,
    HeldPositionSlice,
    MacroSlice,
    Source,
    SourceSlice,
    Subscription,
    build_brief_slice,
    build_held_position_slice,
    build_macro_slice,
    unrealized_pnl_frac,
)
from trader.loop.sources import subscriptions as _subscriptions

log = structlog.get_logger(__name__)

BAR_LOOKBACK = 100
DEFAULT_INTERVALS = ("1h",)


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
    # Exit-review lane (#34): present only when subscribed AND a position is held.
    held_position: HeldPositionSlice | None = None  # advisory; RiskGuard never sees it
    sentiment: SourceSlice | None = None  # its own lane, for a Bot that subscribes to it
    # Every Source slice this context read, for the journal's source_versions.
    sources: tuple[SourceSlice, ...] = ()
    # The decide rules this context is judged by (#126): its Bot version's prompt variant.
    prompt_variant: str = "default"
    # What ``equity`` is: the account's, or a fixed Sleeve's for a Bot that has one.
    equity_label: str = "Account equity"

    @property
    def primary_vol(self) -> float | None:
        """Realized vol for the primary interval — the guard's Kelly scaling input."""
        primary = next(iter(self.indicators.values()), None)
        return primary.realized_vol_20 if primary else None

    @property
    def source_versions(self) -> dict[str, dict[str, Any]]:
        """``{source: {version, as_of}}``: exactly which state each lane read."""
        return {piece.source: piece.stamp() for piece in self.sources}

    def to_prompt(self) -> str:
        """The full user-side prompt. Deterministic given the context (replay-ready)."""
        lines = [
            f"Symbol: {self.symbol.ticker} ({self.symbol.asset_class})",
            f"Time: {self.now.isoformat()}",
            f"Last price: {self.price:,.6g}",
            f"Open position: {self.position_qty:g} units",
            f"{self.equity_label}: {self.equity:,.2f} USD",
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

        if self.sentiment:
            lines += [
                "",
                "X sentiment (advisory — informs direction and conviction only;"
                " a deterministic risk layer owns sizing)"
                f" [via sentiment #{self.sentiment.version}]:",
                f"  {self.sentiment.text}",
            ]

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
                f"  - {d['ts']} {d['trigger']} -> {_history_outcome(d)}"
                for d in self.recent_decisions
            ]
        return "\n".join(lines)


def _history_outcome(row: dict[str, Any]) -> str:
    """What a past decision did, for the prompt's history lines.

    The journal stamps every hold ``shadow`` in any mode (a hold has no order), so the
    raw status would tell a live decider "shadow hold" about its own live holds. A hold
    reads as plain ``hold``; any other action keeps its status, which still says whether
    an open was shadowed, denied, or executed.
    """
    action = (row.get("decision") or {}).get("action", "")
    if action == "hold":
        return "hold"
    return f"{row['status']} {action}".rstrip()


class ContextBuilder:
    """Assembles a DecisionContext. All I/O happens here, none in the decider."""

    def __init__(
        self,
        market_data: MarketData,
        *,
        intervals: tuple[str, ...] = DEFAULT_INTERVALS,
        lookback: int = BAR_LOOKBACK,
        sources: Mapping[str, Source] | None = None,  # name -> Source (sources.build_sources)
        # What build() reads when a call does not name its own subscriptions (a Bot
        # version's ``sources``). Empty = bars and indicators only.
        subscriptions: Iterable[Subscription | Mapping[str, Any]] = (),
    ) -> None:
        self._data = market_data
        self._intervals = intervals
        self._lookback = lookback
        self._sources = dict(sources or {})
        self._subscriptions = _subscriptions(subscriptions)

    async def build(
        self,
        symbol: Symbol,
        *,
        now: datetime,
        triggers: list[Trigger],
        position_qty: float = 0.0,
        equity: float = 0.0,
        recent_decisions: list[dict[str, Any]] | None = None,
        as_of: datetime | None = None,  # None reads current; replay pins to the step
        position: Mapping[str, Any] | None = None,  # positions row (or a replay seed)
        subscriptions: Iterable[Subscription | Mapping[str, Any]] | None = None,
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

        subs = self._subscriptions if subscriptions is None else _subscriptions(subscriptions)
        read: dict[str, SourceSlice] = {}
        for sub in subs:
            source = self._sources.get(sub.source)
            if source is None:
                log.warning("source_not_wired", source=sub.source)
                continue
            # What only the decide knows rides beside the subscription's own params.
            params = {
                **sub.params,
                "now": now,
                "price": price,
                "position": position if position_qty != 0 else None,
            }
            try:
                piece = await source.read(symbol, as_of=as_of, params=params)
            except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
                log.warning(
                    "source_unavailable", source=sub.source, symbol=symbol.ticker, error=str(exc)
                )
                continue
            if piece is not None:
                read[sub.source] = piece

        def detail(name: str) -> Any:
            piece = read.get(name)
            return piece.detail if piece is not None else None

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
            macro_view=detail(MACRO_VIEW),
            brief=detail(BRIEF),
            fred_series=detail(FRED) or (),
            held_position=detail(HELD_POSITION),
            sentiment=read.get(SENTIMENT),
            sources=tuple(read.values()),
        )
