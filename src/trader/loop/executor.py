"""Executor — the only code that talks to a broker, and only about approved intents.

    RiskOutcome.executable() → persist order row → submit → stream fills → ledger

Discipline, each earned in v1:

- **The chokepoint link.** ``execute()`` starts by calling ``outcome.executable()``, which
  raises ``RiskDenied`` unless the chain approved. There is no other entry point and no
  way to hand this class a bare ``OrderIntent``.
- **Persist before submit.** The order row (with its idempotency key) exists in our store
  *before* the venue is told anything, so a crash between submit and ack is recoverable
  rather than invisible.
- **Idempotent by construction.** A fresh UUID ``client_order_id`` per intent; a duplicate
  rejection (HTTP 422) means a previous submit landed, so we adopt that order instead of
  placing a second one.
- **Opens are best-effort, closes must land** (v1:EAI-46). A failed open is journaled and
  dropped; a failed close is journaled loudly because an unmanaged position is the worse
  risk.
- **Crypto cannot take notional orders** at Alpaca (notional needs TIF ``day``; crypto
  only supports ``gtc``/``ioc``), so notional is converted to quantity here using the same
  price the risk chain sized against. A crypto **sell** is then clamped to held qty —
  notional/mark conversion overshoots the fee-adjusted book (live 2026-09-18: two ETH
  closes 403'd requesting 0.196488897 against 0.196485132 available).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from trader.brokers.base import Broker, BrokerError, DuplicateOrder, OrderEvent
from trader.core.symbols import AssetClass, Symbol
from trader.core.types import OrderIntent, Side
from trader.risk.breakers import BreakerStore
from trader.risk.guard import RiskOutcome
from trader.store.audit import audit
from trader.store.db import Database
from trader.store.repos import OrdersRepo, PositionsRepo

log = structlog.get_logger(__name__)

#: Stream event vocabulary -> the status we store on the order row.
EVENT_STATUS = {
    "new": "submitted",
    "accepted": "submitted",
    "pending_new": "submitted",
    "partial_fill": "partially_filled",
    "fill": "filled",
    "canceled": "canceled",
    "expired": "canceled",
    "rejected": "rejected",
    "done_for_day": "canceled",
}

#: Positions that differ from the broker by more than this are drift, not rounding.
DRIFT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ExecutionResult:
    submitted: bool
    client_order_id: str
    order_id: int | None = None
    broker_order_id: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "broker_order_id": self.broker_order_id,
            "reason": self.reason,
        }


def new_client_order_id() -> str:
    """Fresh per intent. Alpaca only guarantees uniqueness against *active* orders, so
    reusing an id from a dead order is undefined — never reuse (findings §3)."""
    return f"trader-{uuid.uuid4()}"


def venue_order_params(
    intent: OrderIntent,
    symbol: Symbol,
    price: float,
    *,
    held_qty: float | None = None,
) -> tuple[float | None, float | None, str]:
    """(qty, notional, time_in_force) as this venue will accept them."""
    if symbol.asset_class is AssetClass.CRYPTO:
        # Crypto: TIF gtc/ioc only, and notional requires day → always send quantity.
        qty = intent.qty if intent.qty is not None else (intent.notional or 0.0) / price
        if intent.side is Side.SELL and held_qty is not None:
            qty = min(qty, held_qty)
        return qty, None, "gtc"
    if intent.notional is not None:
        return None, intent.notional, "day"  # notional requires market + day
    return intent.qty, None, "day"


class Executor:
    def __init__(self, db: Database, broker: Broker) -> None:
        self._db = db
        self._broker = broker
        self._orders = OrdersRepo(db)
        self._positions = PositionsRepo(db)
        self._breakers = BreakerStore(db)

    # -- submit ------------------------------------------------------------

    async def execute(
        self,
        outcome: RiskOutcome,
        symbol: Symbol,
        *,
        price: float,
        decision_id: int | None = None,
    ) -> ExecutionResult:
        """Submit an APPROVED intent. Raises ``RiskDenied`` if it was not approved."""
        intent = outcome.executable()  # the chokepoint: no approval, no order
        client_order_id = new_client_order_id()
        held_qty = None
        if intent.side is Side.SELL:
            row = await self._positions.get(symbol.ticker)
            if row is not None:
                held_qty = abs(float(row["qty"]))
        qty, notional, tif = venue_order_params(intent, symbol, price, held_qty=held_qty)

        # Persist BEFORE the venue knows anything, so a crash mid-submit is recoverable.
        order_id = await self._orders.insert(
            client_order_id=client_order_id,
            symbol=symbol.ticker,
            asset_class=str(symbol.asset_class),
            side=str(intent.side),
            qty=qty,
            notional=notional,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            time_in_force=tif,
            decision_id=decision_id,
        )

        try:
            order = await self._broker.submit_order(
                client_order_id=client_order_id,
                symbol=symbol.for_provider("alpaca_trading"),
                side=str(intent.side),
                qty=qty,
                notional=notional,
                order_type=intent.order_type,
                time_in_force=tif,
                limit_price=intent.limit_price,
            )
        except DuplicateOrder:
            # A previous submit landed: adopt it rather than placing a second order.
            existing = await self._broker.get_order_by_client_id(client_order_id)
            if existing is None:
                await self._fail(order_id, symbol, "duplicate id but no order found")
                return ExecutionResult(False, client_order_id, order_id, reason="duplicate")
            await self._orders.set_status(
                order_id, "submitted", broker_order_id=existing.broker_order_id, raw=existing.raw
            )
            return ExecutionResult(True, client_order_id, order_id, existing.broker_order_id)
        except BrokerError as exc:
            closing = intent.side is Side.SELL
            await self._fail(order_id, symbol, str(exc), closing=closing)
            return ExecutionResult(False, client_order_id, order_id, reason=str(exc))

        await self._orders.set_status(
            order_id, "submitted", broker_order_id=order.broker_order_id, raw=order.raw
        )
        await audit(
            self._db,
            actor="executor",
            category="order",
            action="submitted",
            subject=symbol.ticker,
            payload={
                "client_order_id": client_order_id,
                "broker_order_id": order.broker_order_id,
                "side": str(intent.side),
                "qty": qty,
                "notional": notional,
                "decision_id": decision_id,
            },
        )
        log.info(
            "order_submitted",
            symbol=symbol.ticker,
            client_order_id=client_order_id,
            broker_order_id=order.broker_order_id,
        )
        return ExecutionResult(True, client_order_id, order_id, order.broker_order_id)

    async def _fail(
        self, order_id: int, symbol: Symbol, reason: str, *, closing: bool = False
    ) -> None:
        await self._orders.set_status(order_id, "error", raw={"error": reason})
        await audit(
            self._db,
            actor="executor",
            category="order",
            action="submit_failed",
            subject=symbol.ticker,
            payload={"order_id": order_id, "reason": reason, "closing": closing},
        )
        # An open that didn't go through is a missed opportunity; a close that didn't is
        # an unmanaged position.
        (log.error if closing else log.warning)(
            "order_submit_failed", symbol=symbol.ticker, reason=reason, closing=closing
        )

    # -- stream ------------------------------------------------------------

    async def apply_event(self, event: OrderEvent) -> bool:
        """Fold one trade-update into the ledger. Idempotent on ``execution_id``."""
        row = await self._orders.get_by_client_order_id(event.client_order_id)
        if row is None:
            log.warning("order_event_unknown", client_order_id=event.client_order_id)
            return False

        status = EVENT_STATUS.get(event.event)
        if status:
            await self._orders.set_status(
                row["id"], status, broker_order_id=event.broker_order_id, raw=event.raw
            )

        if event.is_execution and event.fill_price:
            inserted = await self._orders.add_fill(
                row["id"],
                ts=event.ts,
                qty=event.filled_qty,
                price=event.fill_price,
                broker_fill_id=event.execution_id,
                raw=event.raw,
            )
            if inserted == 0:
                return False  # replayed execution, already in the ledger
            await self._apply_fill_to_position(row, event)
        return True

    async def _apply_fill_to_position(self, order_row: dict, event: OrderEvent) -> None:
        symbol = order_row["symbol"]
        signed = event.filled_qty if order_row["side"] == "buy" else -event.filled_qty
        current = await self._positions.get(symbol)
        held = float(current["qty"]) if current else 0.0
        # Venue truth wins when the event carries it: crypto fees come out of the asset,
        # so accumulating gross fill qty overstates the position by the fee (~25 bps —
        # the ledger's first ETH fill drifted exactly that much, 2026-09-14).
        new_qty = event.position_qty if event.position_qty is not None else held + signed

        # Weighted average entry while adding; a reduction keeps the existing basis.
        avg = float(current["avg_entry_price"]) if current and current["avg_entry_price"] else None
        if held == 0 or (held > 0) != (signed > 0):
            avg = event.fill_price if held == 0 else avg
        elif event.fill_price is not None and new_qty != 0:
            avg = ((held * (avg or event.fill_price)) + (signed * event.fill_price)) / new_qty

        await self._positions.upsert(
            symbol=symbol,
            asset_class=order_row["asset_class"],
            qty=new_qty,
            avg_entry_price=avg,
        )
        log.info("fill_applied", symbol=symbol, qty=event.filled_qty, position=new_qty)

    # -- recovery / reconcile ---------------------------------------------

    async def recover_pending(self) -> list[dict[str, Any]]:
        """Resolve orders left mid-flight by a crash, using the idempotency key.

        A row that never reached the venue is closed out as an error; one that did is
        adopted with the venue's current status. This is why the row is written first.
        """
        rows = await self._db.fetch_all(
            "SELECT * FROM orders WHERE status IN ('new', 'submitted') ORDER BY id"
        )
        recovered: list[dict[str, Any]] = []
        for row in rows:
            order = await self._broker.get_order_by_client_id(row["client_order_id"])
            if order is None:
                await self._orders.set_status(
                    row["id"], "error", raw={"error": "not found at venue after restart"}
                )
                recovered.append({"order_id": row["id"], "resolved": "never_landed"})
                continue
            await self._orders.set_status(
                row["id"],
                order.status,
                broker_order_id=order.broker_order_id,
                raw=order.raw,
            )
            recovered.append({"order_id": row["id"], "resolved": order.status})
        if recovered:
            await audit(
                self._db,
                actor="executor",
                category="order",
                action="recovered_pending",
                payload={"orders": recovered},
            )
            log.info("pending_orders_recovered", count=len(recovered))
        return recovered

    async def reconcile_positions(self) -> dict[str, Any]:
        """Compare the local ledger against the venue. Drift PAUSES routing, never crashes.

        A mismatch means we do not know the true book, so the kill switch blocks every
        order (including exits) until an operator clears it.
        """
        try:
            venue = {p.symbol: p.qty for p in await self._broker.get_positions()}
        except BrokerError as exc:
            log.error("reconcile_failed", error=str(exc))
            return {"reconciled": False, "reason": str(exc)}

        local = {row["symbol"]: float(row["qty"]) for row in await self._positions.all()}
        drift = {
            symbol: {"local": local.get(symbol, 0.0), "venue": venue.get(symbol, 0.0)}
            for symbol in set(local) | set(venue)
            if abs(local.get(symbol, 0.0) - venue.get(symbol, 0.0)) > DRIFT_TOLERANCE
        }

        await audit(
            self._db,
            actor="executor",
            category="risk",
            action="reconcile",
            payload={"drift": drift, "venue_positions": venue},
        )
        if drift:
            await self._breakers.set_drift(True)
            log.error("position_drift_detected", drift=drift)
        return {"reconciled": True, "drift": drift}

    async def adopt_venue_positions(self) -> int:
        """Make the local ledger match the venue, then clear the drift flag.

        The operator's escape hatch after investigating a drift: the venue is the source
        of truth for what we actually hold.
        """
        venue = await self._broker.get_positions()
        for position in venue:
            await self._positions.upsert(
                symbol=position.symbol,
                asset_class=position.asset_class or "crypto",
                qty=position.qty,
                avg_entry_price=position.avg_entry_price,
            )
        held = {p.symbol for p in venue}
        for row in await self._positions.all():
            if row["symbol"] not in held and float(row["qty"]) != 0:
                await self._positions.upsert(
                    symbol=row["symbol"], asset_class=row["asset_class"], qty=0.0
                )
        await self._breakers.set_drift(False)
        await audit(
            self._db,
            actor="operator",
            category="risk",
            action="adopt_venue_positions",
            payload={"positions": {p.symbol: p.qty for p in venue}},
        )
        return len(venue)

    # -- flatten -----------------------------------------------------------

    async def flatten_all(self, *, confirm: str) -> dict[str, Any]:
        """Close every position at the venue. Confirm-gated: a typo must not liquidate."""
        if confirm != "FLATTEN":
            return {"flattened": False, "reason": "confirm must be exactly 'FLATTEN'"}
        try:
            orders = await self._broker.close_all_positions()
        except BrokerError as exc:
            log.error("flatten_all_failed", error=str(exc))
            return {"flattened": False, "reason": str(exc)}
        await audit(
            self._db,
            actor="operator",
            category="order",
            action="flatten_all",
            payload={"orders": [o.client_order_id or o.broker_order_id for o in orders]},
        )
        log.warning("flatten_all_submitted", count=len(orders))
        return {"flattened": True, "orders": len(orders)}

    async def run_stream(self, reconnect_delay: float = 5.0) -> None:
        """Consume trade updates forever, reconciling after every (re)connect.

        The venue stream has NO replay — events during a disconnect are lost — so a
        reconcile on connect is the only way the ledger stays true. Disconnects are
        routine: every failure backs off and reconnects; only cancellation ends this.
        (2026-09-11: the first live fill was lost because this raised on first error —
        and was never started at all; LoopRunner now owns it.)
        """
        while True:
            try:
                await self.recover_pending()
                await self.reconcile_positions()
                async for event in self._broker.stream_trade_updates():
                    await self.apply_event(event)
                log.warning("trade_stream_ended", reconnect_in=reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the ledger feed must not die
                log.exception("trade_stream_error", error=str(exc), reconnect_in=reconnect_delay)
            await asyncio.sleep(reconnect_delay)
