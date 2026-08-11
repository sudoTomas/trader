"""Core domain records. Pure frozen dataclasses, no I/O, no framework types.

These are the shapes that flow through the money path
(``SignalScheduler → ContextBuilder → Decider → RiskGuard → Executor → Journal``);
the store rows and LLM output schemas map onto them, never the other way around.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trader.core.symbols import AssetClass


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Action(StrEnum):
    """What the decider proposes for one symbol on one wakeup."""

    HOLD = "hold"
    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True)
class Bar:
    """One OHLCV candle. ``ts`` is the bar's open time, tz-aware UTC."""

    symbol: str
    interval: str  # 1m | 5m | 1h | 1d
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("Bar.ts must be timezone-aware")


@dataclass(frozen=True)
class Quote:
    """Latest bid/ask for one symbol. Providers without a book quote both sides at last."""

    symbol: str
    ts: datetime
    bid: float
    ask: float

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("Quote.ts must be timezone-aware")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class OrderIntent:
    """What the LLM proposes — RiskGuard's input, never the broker's.

    Exactly one of ``qty``/``notional`` is set. Only the RiskGuard chain may turn an
    intent into an order (docs/architecture.md, prime directive).
    """

    symbol: str
    side: Side
    qty: float | None = None
    notional: float | None = None  # USD
    order_type: str = "market"
    limit_price: float | None = None
    time_in_force: str = "gtc"

    def __post_init__(self) -> None:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("OrderIntent needs exactly one of qty or notional")
        if self.qty is not None and self.qty <= 0:
            raise ValueError("OrderIntent.qty must be positive")
        if self.notional is not None and self.notional <= 0:
            raise ValueError("OrderIntent.notional must be positive")


@dataclass(frozen=True)
class Decision:
    """One validated decider output for one symbol on one wakeup."""

    symbol: str
    action: Action
    conviction: float  # 0..1, advisory — sizing is RiskGuard's job
    rationale: str
    intent: OrderIntent | None = None  # None iff action == HOLD

    def __post_init__(self) -> None:
        if not 0.0 <= self.conviction <= 1.0:
            raise ValueError("Decision.conviction must be within [0, 1]")
        if (self.intent is None) != (self.action == Action.HOLD):
            raise ValueError("Decision.intent is required exactly when action != hold")


@dataclass(frozen=True)
class Position:
    """A held position snapshot (mirrors the broker; fills are the ledger)."""

    symbol: str
    asset_class: AssetClass
    qty: float
    avg_entry_price: float | None = None
