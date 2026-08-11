"""Append-only audit trail. ``audit()`` is the ONLY way code touches ``audit_events`` —
insert-only by construction, and the table's trigger refuses UPDATE/DELETE anyway.
"""

from __future__ import annotations

import math
from typing import Any

import structlog
from psycopg.types.json import Jsonb

from trader.store.db import Database

log = structlog.get_logger("audit")


def json_safe(value: Any) -> Any:
    """Make a payload storable as ``jsonb``.

    Postgres JSON cannot represent NaN/Infinity, and an audit write must never fail:
    the events most worth recording are exactly the ones carrying garbage input (a
    denied NaN order size, say). Non-finite floats are preserved as their names so the
    trail shows what was actually received rather than a silent null.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


async def audit(
    db: Database,
    *,
    actor: str,
    category: str,
    action: str,
    subject: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Record a state-changing action: one immutable row + one structured log line."""
    safe_payload = json_safe(payload or {})
    row = await db.fetch_one(
        """
        INSERT INTO audit_events (actor, category, action, subject, payload)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (actor, category, action, subject, Jsonb(safe_payload)),
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    log.info(
        "audit_event",
        audit_id=row["id"],
        actor=actor,
        category=category,
        action=action,
        subject=subject,
        payload=safe_payload,
    )
    return row["id"]
