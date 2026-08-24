"""Research Desk part 1 — the operator's capture layer (EAI-177).

Hand-picked macro research enters here from any surface. Notes are immutable markdown:
content never changes after submit, only lifecycle status does. **Capture once, compile
async** (the ai-quoting audit lesson) — nobody hand-edits the compiled knowledge base,
and part 2's compiler reads only what ``list()`` returns.

Auto-stale: every note gets an ``expires_at`` window at submit (default
``TRADER_RESEARCH_STALE_DAYS``) unless pinned/evergreen. An expired note silently drops
out of compilation but keeps its status — retiring is an explicit operator act. The
ai-quoting gap was having *no* staleness; the v1 macro-lab gap was analytics nobody
audited. Both inform the shape here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trader.store.audit import audit
from trader.store.db import Database
from trader.store.repos import ResearchNotesRepo

Row = dict[str, Any]


class ResearchDesk:
    """Submit / list / retire over ``research_notes``, with the audit trail built in."""

    def __init__(self, db: Database, *, stale_days: int | None = None) -> None:
        if stale_days is None:
            from trader.ops.config import get_settings

            stale_days = get_settings().research_stale_days
        self._db = db
        self._repo = ResearchNotesRepo(db)
        self._stale_days = stale_days

    async def submit(
        self,
        *,
        title: str,
        body: str,
        tags: list[str] | None = None,
        asset_classes: list[str] | None = None,
        symbols: list[str] | None = None,
        source: str | None = None,
        pinned: bool = False,
        stale_days: int | None = None,
    ) -> Row:
        """Store one immutable note (body verbatim, byte-for-byte) and audit the capture."""
        if not title.strip():
            raise ValueError("a research note needs a title")
        if not body.strip():
            raise ValueError("a research note needs a body")
        window = self._stale_days if stale_days is None else stale_days
        # Schema invariant: expires_at is NULL only when pinned (evergreen).
        expires_at = None if pinned else datetime.now(UTC) + timedelta(days=window)
        note_id = await self._repo.add(
            title=title,
            body=body,
            tags=tags,
            asset_classes=asset_classes,
            symbols=symbols,
            source=source,
            pinned=pinned,
            expires_at=expires_at,
        )
        await audit(
            self._db,
            actor="operator",
            category="research",
            action="submit_note",
            subject=f"note:{note_id}",
            payload={"title": title, "source": source, "pinned": pinned, "stale_days": window},
        )
        row = await self._repo.get(note_id)
        assert row is not None
        return row

    async def list(
        self,
        *,
        include_stale: bool = False,
        include_retired: bool = False,
        limit: int = 100,
    ) -> list[Row]:
        """Default view = what compilation sees: active/disputed and inside the window."""
        return await self._repo.list_notes(
            include_stale=include_stale, include_retired=include_retired, limit=limit
        )

    async def retire(self, note_id: int) -> bool:
        """Explicitly retire a note. False when it doesn't exist or is already retired."""
        retired = await self._repo.retire(note_id)
        if retired:
            await audit(
                self._db,
                actor="operator",
                category="research",
                action="retire_note",
                subject=f"note:{note_id}",
            )
        return retired
