"""Append-only audit trail. ``audit()`` is the ONLY way code touches ``audit_events`` —
insert-only by construction, and the table's trigger refuses UPDATE/DELETE anyway.
"""

from __future__ import annotations

from typing import Any

import structlog
from psycopg.types.json import Jsonb

from trader.store.db import Database

log = structlog.get_logger("audit")


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
    row = await db.fetch_one(
        """
        INSERT INTO audit_events (actor, category, action, subject, payload)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (actor, category, action, subject, Jsonb(payload or {})),
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    log.info(
        "audit_event",
        audit_id=row["id"],
        actor=actor,
        category=category,
        action=action,
        subject=subject,
        payload=payload or {},
    )
    return row["id"]
