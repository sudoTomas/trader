"""Shared fixtures. Store tests run against the real compose Postgres (:5544) —
`docker compose -f infra/docker-compose.yml up -d db` first. Tables are truncated before
every test (v1 lesson: rows persisted across tests broke ordering assumptions).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest

os.environ.setdefault("TRADER_AUTH_DISABLED", "true")

from trader.store.db import Database  # noqa: E402  (env default must be set first)

# Every table the baseline migration creates, except schema_migrations bookkeeping.
# TRUNCATE is allowed on audit_events (its append-only trigger blocks row-level
# UPDATE/DELETE only) — fine for tests, and no such statement exists in app code.
ALL_TABLES = (
    "fills",
    "orders",
    "approvals",
    "decisions",
    "positions",
    "audit_events",
    "research_notes",
    "macro_views",
    "bars",
    "usage_daily",
)


@pytest.fixture(scope="session")
def _migrated() -> None:
    """Apply migrations once per test session, on a throwaway pool in its own loop."""
    from trader.store.migrate import run_migrations

    async def _run() -> None:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            await run_migrations(db)
        finally:
            await db.close()

    asyncio.run(_run())


@pytest.fixture
def clean_db(_migrated: None) -> None:
    """Truncate all tables for a SYNC test (surface tests drive async code via TestClient)."""

    async def _truncate() -> None:
        database = Database(min_size=1, max_size=1)
        await database.open()
        try:
            await database.execute(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
        finally:
            await database.close()

    asyncio.run(_truncate())


@pytest.fixture
async def db(_migrated: None) -> AsyncIterator[Database]:
    """A fresh pool per test (so it lives on this test's event loop), tables truncated."""
    database = Database(min_size=1, max_size=2)
    await database.open()
    await database.execute(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
    yield database
    await database.close()
