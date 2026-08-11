"""Broker protocol — the venue-facing contract.

Deliberately narrow: the executor is the only caller, and it may only act on an intent
that RiskGuard approved. Keeping this a protocol (rather than an Alpaca-shaped class)
preserves the documented IBKR add-back path (v1:adapters/broker/{ibkr,reconcile}.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class BrokerError(RuntimeError):
    """The venue could not be reached or refused the request."""


class DuplicateOrder(BrokerError):
    """This ``client_order_id`` is already live at the venue.

    Not a failure: it means a previous submit landed (the idempotency key did its job),
    so the caller must reconcile rather than resubmit.
    """


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float
    non_marginable_buying_power: float
    status: str
    tradable: bool  # status ACTIVE and no blocks — the health predicate

    @property
    def healthy(self) -> bool:
        return self.tradable


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float  # signed
    qty_available: float  # qty minus open orders — the correct size for a close
    avg_entry_price: float | None = None
    asset_class: str | None = None


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    status: str  # Alpaca REST vocabulary: new, partially_filled, filled, canceled, ...
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in {"filled", "canceled", "expired", "rejected", "done_for_day"}


@dataclass(frozen=True)
class OrderEvent:
    """One trade-update. ``event`` uses the STREAM vocabulary (``partial_fill``), which
    differs from the REST status vocabulary (``partially_filled``)."""

    event: str
    client_order_id: str
    broker_order_id: str
    status: str
    ts: datetime
    filled_qty: float = 0.0
    fill_price: float | None = None
    execution_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_execution(self) -> bool:
        return self.event in {"fill", "partial_fill"}


@runtime_checkable
class Broker(Protocol):
    name: str

    async def is_healthy(self) -> bool: ...

    async def get_account(self) -> AccountSnapshot: ...

    async def get_positions(self) -> list[BrokerPosition]: ...

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    async def submit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
        order_type: str = "market",
        time_in_force: str = "gtc",
        limit_price: float | None = None,
    ) -> BrokerOrder: ...

    async def cancel_order(self, broker_order_id: str) -> None: ...

    async def close_position(self, symbol: str) -> BrokerOrder | None: ...

    def stream_trade_updates(self) -> AsyncIterator[OrderEvent]: ...
