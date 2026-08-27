"""Journal — every wakeup writes a row, including the ones that decided nothing.

A decision you can't replay is a decision you can't audit. Each row carries the exact
prompt the model saw, its raw output, the risk verdicts, cost and latency, and (from M4)
the macro-view version — enough to re-run the call offline and compare.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import structlog

from trader.core.types import Decision
from trader.store.audit import json_safe
from trader.store.db import Database
from trader.store.repos import DecisionsRepo

log = structlog.get_logger(__name__)

# Row statuses. `shadow` = decided but deliberately not submitted (shadow mode);
# `suppressed` = a wakeup that produced no decision (e.g. no usable market data).
SHADOW = "shadow"
PROPOSED = "proposed"
DENIED = "denied"
EXECUTED = "executed"
ERROR = "error"
SUPPRESSED = "suppressed"


def decision_to_json(decision: Decision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return json_safe(asdict(decision))


class Journal:
    def __init__(self, db: Database) -> None:
        self._repo = DecisionsRepo(db)

    async def record(
        self,
        *,
        trigger: str,
        status: str,
        symbol: str | None = None,
        asset_class: str | None = None,
        prompt: str | None = None,
        raw_output: str | None = None,
        decision: Decision | None = None,
        risk_verdicts: list[dict[str, Any]] | None = None,
        desk_verdicts: dict[str, Any] | None = None,
        macro_view_version: int | None = None,
        brief_version: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
    ) -> int:
        decision_id = await self._repo.insert(
            trigger=trigger,
            status=status,
            symbol=symbol,
            asset_class=asset_class,
            prompt=prompt,
            raw_output=raw_output,
            decision=decision_to_json(decision),
            risk_verdicts=json_safe(risk_verdicts) if risk_verdicts is not None else None,
            desk_verdicts=json_safe(desk_verdicts) if desk_verdicts is not None else None,
            macro_view_version=macro_view_version,
            brief_version=brief_version,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
        log.info(
            "decision_journaled",
            decision_id=decision_id,
            symbol=symbol,
            trigger=trigger,
            status=status,
            action=decision.action if decision else None,
        )
        return decision_id

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._repo.recent(limit)
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]

    async def get(self, decision_id: int) -> dict[str, Any] | None:
        row = await self._repo.get(decision_id)
        return None if row is None else {**row, "ts": row["ts"].isoformat()}
