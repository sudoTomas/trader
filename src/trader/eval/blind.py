"""Blinded replay (#140): how much of replay P&L is the model remembering the names.

A model trained past the replay window may know what AAPL did in October. A **blind**
replay renders every prompt the run sends (decide, the desk's analysts and critic, the
Confirm and the exit-review block all read ``DecisionContext.to_prompt``) without the
three things that let it recall:

- **identity** — the ticker becomes a stable per-run alias (``ASSET_A``); the asset
  class stays, since the decide rules depend on it;
- **calendar dates and times** — the step is ``bar t``, earlier bars ``bar t-k``, and
  FRED prints and sentiment polls are dated relative to it;
- **dollar values** — prices are an index (the run's first bar = 100); positions,
  P&L and sizes are percent of the Sleeve.

The response contract is unchanged: the same JSON, with ``notional_usd`` read as a
percent of the Sleeve (the prompt says so). The harness maps it back to dollars
(``Blinder.unblind``) after the desk and before the crude book, RiskGuard's seat, sees the
intent, so the stop, the min hold, the no-trade band and the cost model work unchanged.
The model never names a symbol, so the alias needs no mapping: the intent is built for
the run's real symbol.

Sources whose free text names the asset (the Macro View, briefs) are **dropped**, not
scrubbed: a scrub that misses one nickname leaks the whole run. FRED, X sentiment and
the held position stay, re-rendered from their numbers alone (sentiment's score, never
its free-text volume or drivers). The run summary records which Sources were dropped.
Dropping changes what the model reads, so a named-vs-blind gap measures recall *and*
those lanes together; compare against a named run with the same Sources dropped to
separate them.

Blinding "reduces recall rather than removing it" (ai-hedge-fund's phrasing): the
shape of a series can still be recognisable. Live prompts are untouched: only a blind
replay sets ``DecisionContext.blind``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from trader.core.symbols import Symbol
from trader.core.types import Decision
from trader.loop.context import DecisionContext
from trader.loop.sources import (
    EXIT_REVIEW_ASK,
    FRED,
    HELD_POSITION,
    SENTIMENT,
    Subscription,
    subscriptions,
    unrealized_pnl_frac,
)

DEFAULT_ALIAS = "ASSET_A"
#: The Sources a blind run keeps: each re-renders from numbers alone. Every other Source
#: (the Macro View, briefs, and any added later until it is vetted here) is dropped.
BLIND_SOURCES = frozenset({FRED, SENTIMENT, HELD_POSITION})

UNITS_NOTE = (
    "Units: identity, calendar dates and dollar values are withheld. Prices are an index"
    " (the run's first bar = 100); positions and P&L are percent of the Sleeve. Every"
    " `notional_usd` in this conversation (yours, a draft's, a critic's) means percent of"
    " the Sleeve (10 = 10% of the Sleeve), not dollars."
)


def split_sources(
    spec: Iterable[Subscription | Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """A run's subscriptions as blind replay reads them: (the kept specs, the dropped
    Source names, sorted)."""
    kept: list[dict[str, Any]] = []
    dropped: set[str] = set()
    for sub in subscriptions(spec):
        if sub.source in BLIND_SOURCES:
            kept.append(sub.as_spec())
        else:
            dropped.add(sub.source)
    return kept, sorted(dropped)


def identity_terms(symbol: Symbol) -> list[str]:
    """The spellings that name the symbol: the ticker, every provider's spelling, and a
    pair's base asset (``BTC`` of ``BTC/USD``)."""
    terms = {symbol.ticker, *symbol.provider_symbols.values()}
    if "/" in symbol.ticker:
        terms.add(symbol.ticker.split("/", 1)[0])
    return sorted(terms)


def names_symbol(text: str, symbol: Symbol) -> bool:
    """Whether ``text`` names the symbol as a whole word (a blind run refuses rules that
    do)."""
    return any(
        re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text)
        for term in identity_terms(symbol)
    )


def _ago(delta: timedelta) -> str:
    hours = max(int(delta.total_seconds()) // 3600, 0)
    if hours >= 48:
        return f"{hours // 24} days"
    return f"{hours}h"


@dataclass(frozen=True)
class BlindView:
    """One step's blind renderer; ``DecisionContext.to_prompt`` defers to it."""

    alias: str
    base: float  # the index base: the run's first bar's close (= 100)
    step: timedelta  # the replay step, for relative bar labels
    position: Mapping[str, Any] | None = None  # the sim's book: qty, avg_entry_price, opened_at

    def index(self, price: float | None) -> str:
        if price is None:
            return "None"
        return f"{price / self.base * 100:.6g}"

    def pct_of_sleeve(self, usd: float, equity: float) -> str:
        return f"{usd / equity * 100:.2f}%" if equity > 0 else "n/a"

    def bars_before(self, now: datetime, ts: datetime) -> str:
        k = round((now - ts) / self.step)
        return "bar t" if k <= 0 else f"bar t-{k}"

    def render(self, ctx: DecisionContext) -> str:
        lines = [
            f"Asset: {self.alias} ({ctx.symbol.asset_class})",
            "Time: bar t (the current step; bar t-k is k steps earlier)",
            f"Last price: {self.index(ctx.price)} (index)",
            f"Open position: {self._position_line(ctx)}",
            UNITS_NOTE,
            "",
            "Wake triggers:",
        ]
        # The trigger's detail names the symbol (and a price for some): its type only.
        lines += [f"  - {t.type}" for t in ctx.triggers] or ["  - (none)"]

        for interval, indicators in ctx.indicators.items():
            bars = ctx.bars.get(interval, [])
            values = indicators.as_dict()
            lines += ["", f"Interval {interval} ({len(bars)} bars):"]
            lines += [
                f"  rsi_14: {values['rsi_14']}",
                f"  sma_20: {self.index(values['sma_20'])} (index)",
                f"  ema_12: {self.index(values['ema_12'])} (index)",
                f"  realized_vol_20: {values['realized_vol_20']}",
            ]
            if bars:
                closes = ", ".join(self.index(b.close) for b in bars[-10:])
                lines.append(f"  last 10 closes (index, oldest first): {closes}")

        if ctx.fred_series:
            lines += [
                "",
                "FRED macro series (latest print on or before this time; facts only,"
                " not a forecast — a deterministic risk layer owns sizing):",
            ]
            for point in ctx.fred_series:
                days = (ctx.now.date() - point.obs_date).days
                when = "same day as bar t" if days <= 0 else f"{days} days before bar t"
                lines.append(f"  {point.series_id}: {point.value:,.6g} ({when})")

        sentiment = self._sentiment_line(ctx, ctx.sentiment.detail if ctx.sentiment else None)
        if sentiment:
            lines += [
                "",
                "X sentiment (advisory — informs direction and conviction only;"
                " a deterministic risk layer owns sizing):",
                f"  {sentiment}",
            ]

        if ctx.analysts:
            # Written by the desk's analysts from this same blind prompt.
            lines += [
                "",
                "Analyst desk (advisory reads — contradictions between analysts are"
                " information, not noise; a deterministic risk layer owns sizing):",
            ]
            lines += [f"  {entry}" for entry in ctx.analysts]

        held = self._held_entries(ctx)
        if held:
            lines += [
                "",
                "Held position review (advisory — the thesis question is yours;"
                " a deterministic risk layer owns sizing):",
            ]
            lines += [f"  {entry}" for entry in held]

        if ctx.recent_decisions:
            lines += ["", "Recent decisions (newest first):"]
            for d in ctx.recent_decisions:
                ts = d["ts"] if isinstance(d["ts"], datetime) else datetime.fromisoformat(d["ts"])
                action = (d.get("decision") or {}).get("action", "")
                outcome = "hold" if action == "hold" else f"{d['status']} {action}".rstrip()
                lines.append(f"  - {self.bars_before(ctx.now, ts)} {d['trigger']} -> {outcome}")
        return "\n".join(lines)

    # -- lanes ---------------------------------------------------------------------

    def _position_line(self, ctx: DecisionContext) -> str:
        if ctx.position_qty == 0:
            return "none"
        side = "long" if ctx.position_qty > 0 else "short"
        value = abs(ctx.position_qty) * ctx.price
        return f"{side}, {self.pct_of_sleeve(value, ctx.equity)} of the Sleeve"

    def _sentiment_line(self, ctx: DecisionContext, row: Mapping[str, Any] | None) -> str | None:
        """The score alone, dated relative to the step: the poll's free-text volume and
        drivers can name the asset."""
        if row is None or row.get("score") is None:
            return None
        try:
            score = float(row["score"])
        except (TypeError, ValueError):
            return None
        line = f"Latest X sentiment: {score:+.2f}"
        if isinstance(row.get("ts"), datetime):
            line += f" ({_ago(ctx.now - row['ts'])} before bar t)"
        return line

    def _held_entries(self, ctx: DecisionContext) -> list[str]:
        """The exit-review block, from the sim's book: entry on the index, age, P&L as a
        percent of entry and of the Sleeve, the sentiment score and the ask."""
        if ctx.held_position is None or self.position is None:
            return []
        qty = float(self.position.get("qty") or 0.0)
        entry = float(self.position.get("avg_entry_price") or 0.0)
        if qty == 0.0 or entry <= 0.0:
            return []
        side = "long" if qty > 0 else "short"
        entries = [
            f"Entry: {self.index(entry)} on the price index ({side},"
            f" {self.pct_of_sleeve(abs(qty) * entry, ctx.equity)} of the Sleeve at entry)"
        ]
        opened_at = self.position.get("opened_at")
        if isinstance(opened_at, datetime):
            entries.append(f"Position age: {_ago(ctx.now - opened_at)}")
        pnl = unrealized_pnl_frac(ctx.price, qty, entry)
        if pnl is not None:
            usd = (ctx.price - entry) * qty
            entries.append(
                f"Unrealized P&L: {pnl:+.2%} vs entry ({self.pct_of_sleeve(usd, ctx.equity)}"
                f" of the Sleeve; mark {self.index(ctx.price)})"
            )
        sentiment = self._sentiment_line(ctx, ctx.held_position.sentiment)
        if sentiment:
            entries.append(sentiment)
        entries.append(EXIT_REVIEW_ASK)
        return entries


class Blinder:
    """One blind run's alias and price index. ``blind`` puts the renderer on each
    step's context; ``unblind`` maps the model's %-of-Sleeve size back to dollars."""

    def __init__(self, symbol: Symbol, *, step: timedelta, alias: str = DEFAULT_ALIAS) -> None:
        self.symbol = symbol
        self.alias = alias
        self._step = step
        self._base: float | None = None

    def blind(
        self, ctx: DecisionContext, *, position: Mapping[str, Any] | None = None
    ) -> DecisionContext:
        if self._base is None:
            # The run's first bar is the index's 100 for the whole run, so every step
            # (and the recent-decision history) reads on one scale.
            primary = next(iter(ctx.bars.values()), [])
            self._base = primary[0].close if primary and primary[0].close > 0 else ctx.price
        view = BlindView(self.alias, self._base, self._step, position)
        return replace(ctx, blind=view)

    def unblind(self, decision: Decision, *, equity: float) -> Decision:
        """The same Decision with its notional in dollars: the model's number is a
        percent of the Sleeve (``equity``)."""
        if decision.intent is None or decision.intent.notional is None:
            return decision
        notional = decision.intent.notional / 100.0 * equity
        return replace(decision, intent=replace(decision.intent, notional=notional))
