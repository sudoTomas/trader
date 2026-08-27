"""ContextBuilder — everything the decider sees, assembled before the LLM is called.

**No LLM tool calls in this hot path.** v1 bound 28 MCP tools into the decide call and
got 120 s timeouts (v1:TRA-42); here the context is pre-fetched, indicators are computed
locally (v1 fetched them over MCP and got null for 987/987 decisions), and the prompt is
a finished string by the time the model runs.

Fetch-on-tick: bars come from the provider and are used in memory. Nothing is stored.
The Macro View slice occupies its own budgeted lane in the prompt (EAI-178): advisory
by construction — it feeds the decider only, never RiskGuard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
    """Render a brief into whole entries under its own character budget (EAI-192).

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
    analysts: list[str] = field(default_factory=list)  # desk lanes (EAI-190); advisory

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
    ) -> None:
        self._data = market_data
        self._intervals = intervals
        self._lookback = lookback
        self._macro_views = macro_views
        self._macro_budget = macro_budget_chars
        self._briefs = briefs
        self._brief_budget = brief_budget_chars

    async def build(
        self,
        symbol: Symbol,
        *,
        now: datetime,
        triggers: list[Trigger],
        position_qty: float = 0.0,
        equity: float = 0.0,
        recent_decisions: list[dict[str, Any]] | None = None,
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

        price = 0.0
        primary = bars.get(self._intervals[0], [])
        if primary:
            price = primary[-1].close
        else:
            # No bars: fall back to a quote so the guard still sees a real price.
            try:
                price = (await self._data.get_latest_quote(symbol)).mid
            except MarketDataError as exc:
                log.warning("context_price_unavailable", symbol=symbol.ticker, error=str(exc))

        macro_view: MacroSlice | None = None
        if self._macro_views is not None:
            try:
                current = await self._macro_views.current()
                if current is not None:
                    macro_view = build_macro_slice(
                        current, symbol.asset_class, budget_chars=self._macro_budget
                    )
            except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
                log.warning("macro_view_unavailable", error=str(exc))

        brief: BriefSlice | None = None
        if self._briefs is not None:
            try:
                current_brief = await self._briefs.current(symbol.ticker)
                if current_brief is not None:
                    brief = build_brief_slice(current_brief, budget_chars=self._brief_budget)
            except Exception as exc:  # noqa: BLE001 - advisory must never block a decide
                log.warning("brief_unavailable", error=str(exc))

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
        )
