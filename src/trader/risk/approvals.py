"""Human-in-the-loop approvals — the HITL mechanism (an approvals table, not a graph
interrupt; docs/architecture.md).

Opens the operator opted to park (above ``park_above_usd``) land here instead of
executing. An approval that a human later grants is **not** a licence to trade at any
price: redemption re-validates freshness and price, because an approval granted against
a stale quote is exactly how v1 produced stale fills (see docs/lessons.md#approval-price-tolerance).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from trader.core.types import OrderIntent, Side
from trader.store.db import Database
from trader.store.repos import ApprovalsRepo

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Redemption:
    """The outcome of trying to turn an approved row back into a live intent."""

    ok: bool
    intent: OrderIntent | None = None
    reason: str = ""


def intent_to_payload(intent: OrderIntent, price: float) -> dict[str, Any]:
    """The parked intent plus the price it was sized against (the staleness anchor)."""
    return {**asdict(intent), "parked_price": price}


def payload_to_intent(payload: dict[str, Any]) -> OrderIntent:
    fields = {k: v for k, v in payload.items() if k != "parked_price"}
    return OrderIntent(**{**fields, "side": Side(fields["side"])})


class ApprovalService:
    """Parks intents and redeems approved ones under freshness + price validity."""

    def __init__(self, db: Database) -> None:
        self._repo = ApprovalsRepo(db)

    async def park(
        self, intent: OrderIntent, *, price: float, reason: str, decision_id: int | None = None
    ) -> int:
        approval_id = await self._repo.create(
            order_intent=intent_to_payload(intent, price),
            reason=reason,
            decision_id=decision_id,
        )
        log.info(
            "order_parked_for_approval",
            approval_id=approval_id,
            symbol=intent.symbol,
            reason=reason,
        )
        return approval_id

    async def pending(self) -> list[dict[str, Any]]:
        return await self._repo.pending()

    async def resolve(self, approval_id: int, *, approved: bool, resolved_by: str) -> bool:
        return await self._repo.resolve(
            approval_id, status="approved" if approved else "denied", resolved_by=resolved_by
        )

    async def redeem(
        self,
        approval_id: int,
        *,
        current_price: float,
        ttl_seconds: int,
        price_tolerance_frac: float,
        now: datetime | None = None,
    ) -> Redemption:
        """Turn an approved row back into an executable intent, or explain why not."""
        row = await self._repo.get(approval_id)
        if row is None:
            return Redemption(False, reason="approval not found")
        if row["status"] != "approved":
            return Redemption(False, reason=f"approval is {row['status']}, not approved")

        now = now or datetime.now(tz=UTC)
        age = (now - row["created_at"]).total_seconds()
        if age > ttl_seconds:
            return Redemption(False, reason=f"approval expired ({age:.0f}s > {ttl_seconds}s)")

        payload = row["order_intent"]
        parked_price = payload.get("parked_price")
        if not parked_price or current_price <= 0:
            return Redemption(False, reason="unreadable price on redemption")
        drift = abs(current_price - parked_price) / parked_price
        if drift > price_tolerance_frac:
            return Redemption(
                False,
                reason=(
                    f"price moved {drift:.2%} since approval (tolerance {price_tolerance_frac:.2%})"
                ),
            )
        return Redemption(True, intent=payload_to_intent(payload))
