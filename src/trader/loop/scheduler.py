"""SignalScheduler — deterministic wake triggers. Pure, no I/O.

Event-driven rather than fixed-interval: the trader wakes when something happened
(price moved, vol spiked, a position is bleeding) plus a heartbeat floor.

THE cooldown discipline (see docs/lessons.md#cooldowns-start-on-dispatch): ``evaluate()``
updates baselines but never starts cooldowns; ``mark_dispatched()`` does, and only when a
wakeup actually launches. A trigger suppressed because a run was already in flight
therefore re-fires on the next tick instead of being silently swallowed by a cooldown it
never used.

Cooldowns are keyed per symbol for symbol-scoped triggers, so one symbol's dispatch can
never suppress another symbol's independent signal.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trader.core.symbols import AssetClass, Symbol

HEARTBEAT = "heartbeat"
PRICE_MOVE = "price_move"
VOL_SPIKE = "vol_spike"
POSITION_RISK = "position_risk"
SENTIMENT = "sentiment"

#: Trigger types scoped to one symbol (cooldowns keyed by symbol).
SYMBOL_SCOPED = frozenset({PRICE_MOVE, VOL_SPIKE, POSITION_RISK, SENTIMENT})


@dataclass(frozen=True)
class Trigger:
    type: str
    symbol: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def cooldown_key(self) -> str:
        return f"{self.type}:{self.symbol}" if self.type in SYMBOL_SCOPED else self.type


@dataclass(frozen=True)
class Mark:
    """What the scheduler observes per symbol per tick — cheap, fetch-on-tick."""

    price: float
    realized_vol: float | None = None
    unrealized_pnl_frac: float | None = None  # signed, vs entry
    # Latest live X sentiment in [-1, 1]. None when unavailable/degraded — a degraded
    # read must never trigger (a citation-free score is model prior, not live X — see
    # docs/lessons.md#sentiment-no-citations-is-unavailable). The runner populates this for held
    # symbols only.
    sentiment_score: float | None = None


@dataclass(frozen=True)
class TriggerConfig:
    # Heartbeat cadence per asset class: crypto trades 24/7 so it wakes more often.
    heartbeat_seconds: dict[AssetClass, int] = field(
        default_factory=lambda: {
            AssetClass.CRYPTO: 900,
            AssetClass.US_EQUITY: 1800,
            AssetClass.COMMODITY_ETF: 1800,
        }
    )
    price_move_pct: float = 0.02  # over the trailing window
    price_window_seconds: int = 3600
    vol_spike_mult: float = 2.0  # vs the trailing mean of observed vols
    position_risk_pct: float = 0.05  # unrealized move that demands a look
    cooldown_price_move: int = 900
    cooldown_vol_spike: int = 1800
    cooldown_position_risk: int = 600
    # Shadow-first: the sentiment trigger stays off until ≥1 week of journaled scores
    # earns the operator's flip of TRADER_SENTIMENT_TRIGGER_ENABLED
    # (see docs/lessons.md#sentiment-held-symbols-only).
    sentiment_enabled: bool = False
    sentiment_swing: float = 0.5  # |delta| in score units ([-1, 1]) that wakes the trader
    cooldown_sentiment: int = 3600


class SignalScheduler:
    """Decides when to wake. One instance per process, mutated only through its methods."""

    def __init__(self, symbols: list[Symbol], config: TriggerConfig | None = None) -> None:
        self._symbols = {s.ticker: s for s in symbols}
        self._config = config or TriggerConfig()
        self._prices: dict[str, deque[tuple[datetime, float]]] = {}
        self._vols: dict[str, deque[float]] = {}
        self._sentiments: dict[str, float] = {}
        self._last_fired: dict[str, datetime] = {}
        self._last_heartbeat: dict[str, datetime] = {}

    def evaluate(self, now: datetime, marks: dict[str, Mark]) -> list[Trigger]:
        """Triggers firing this tick. Updates baselines; does NOT start cooldowns."""
        fired: list[Trigger] = []
        for ticker, mark in marks.items():
            symbol = self._symbols.get(ticker)
            if symbol is None or not symbol.market_calendar.is_open(now):
                continue  # calendar gating: a closed venue cannot be traded
            fired.extend(self._for_symbol(symbol, mark, now))
        return [t for t in fired if not self._in_cooldown(t, now)]

    def mark_dispatched(self, triggers: list[Trigger], now: datetime) -> None:
        """Start cooldowns — called ONLY when a wakeup actually launches."""
        for trigger in triggers:
            self._last_fired[trigger.cooldown_key] = now
            if trigger.type == HEARTBEAT:
                self._last_heartbeat[trigger.symbol] = now

    def _for_symbol(self, symbol: Symbol, mark: Mark, now: datetime) -> list[Trigger]:
        triggers: list[Trigger] = []
        cfg = self._config

        # Heartbeat: a floor on attention, per asset class.
        cadence = cfg.heartbeat_seconds.get(symbol.asset_class, 1800)
        last = self._last_heartbeat.get(symbol.ticker)
        if last is None or (now - last) >= timedelta(seconds=cadence):
            triggers.append(Trigger(HEARTBEAT, symbol.ticker, {"cadence_seconds": cadence}))

        # Price move over the trailing window.
        if mark.price > 0:
            history = self._prices.setdefault(symbol.ticker, deque(maxlen=512))
            history.append((now, mark.price))
            window = [
                p for ts, p in history if (now - ts) <= timedelta(seconds=cfg.price_window_seconds)
            ]
            if len(window) >= 2 and window[0] > 0:
                pct = (mark.price - window[0]) / window[0]
                if abs(pct) >= cfg.price_move_pct:
                    triggers.append(
                        Trigger(
                            PRICE_MOVE,
                            symbol.ticker,
                            {"pct": round(pct, 5), "from": window[0], "to": mark.price},
                        )
                    )

        # Volatility spike vs the trailing mean of what we've observed.
        if mark.realized_vol is not None and mark.realized_vol > 0:
            vols = self._vols.setdefault(symbol.ticker, deque(maxlen=48))
            trailing = list(vols)
            vols.append(mark.realized_vol)
            if len(trailing) >= 2:
                mean = sum(trailing) / len(trailing)
                if mean > 0 and mark.realized_vol >= cfg.vol_spike_mult * mean:
                    triggers.append(
                        Trigger(
                            VOL_SPIKE,
                            symbol.ticker,
                            {"vol": mark.realized_vol, "trailing_mean": mean},
                        )
                    )

        # Material sentiment swing on live X reads. First observation is baseline only;
        # a None score (degraded/unavailable) neither triggers nor moves the baseline.
        if cfg.sentiment_enabled and mark.sentiment_score is not None:
            prev = self._sentiments.get(symbol.ticker)
            self._sentiments[symbol.ticker] = mark.sentiment_score
            if prev is not None:
                delta = mark.sentiment_score - prev
                if abs(delta) >= cfg.sentiment_swing:
                    triggers.append(
                        Trigger(
                            SENTIMENT,
                            symbol.ticker,
                            {"delta": round(delta, 4), "from": prev, "to": mark.sentiment_score},
                        )
                    )

        # A position moving against (or hard for) us deserves a look.
        if (
            mark.unrealized_pnl_frac is not None
            and abs(mark.unrealized_pnl_frac) >= cfg.position_risk_pct
        ):
            triggers.append(
                Trigger(POSITION_RISK, symbol.ticker, {"unrealized": mark.unrealized_pnl_frac})
            )
        return triggers

    def _cooldown_seconds(self, trigger_type: str) -> int:
        cfg = self._config
        return {
            HEARTBEAT: 0,  # its own cadence governs
            PRICE_MOVE: cfg.cooldown_price_move,
            VOL_SPIKE: cfg.cooldown_vol_spike,
            POSITION_RISK: cfg.cooldown_position_risk,
            SENTIMENT: cfg.cooldown_sentiment,
        }.get(trigger_type, 0)

    def _in_cooldown(self, trigger: Trigger, now: datetime) -> bool:
        seconds = self._cooldown_seconds(trigger.type)
        if seconds <= 0:
            return False
        fired_at = self._last_fired.get(trigger.cooldown_key)
        return fired_at is not None and (now - fired_at) < timedelta(seconds=seconds)
