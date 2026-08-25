"""Shared fixtures. Store tests run against the real compose Postgres (:5544) —
`docker compose -f infra/docker-compose.yml up -d db` first. Tables are truncated before
every test (v1 lesson: rows persisted across tests broke ordering assumptions).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator

import pytest
import structlog

os.environ.setdefault("TRADER_AUTH_DISABLED", "true")
# Tests must be immune to the operator's .env: never build the live loop (and with it a
# real broker) off ambient config. Real env vars outrank the .env file in pydantic-settings.
os.environ["TRADER_LOOP_ENABLED"] = "false"
os.environ["TRADER_SHADOW_MODE"] = "true"

# The suite truncates tables wholesale, and the deployed trader may share the compose
# Postgres — running tests against its database wipes the live journal/ledger (it did
# once, 2026-08-25). Redirect every test to a *_test sibling database, created on demand.
_BASE_DB_URL = os.environ.get(
    "TRADER_DATABASE_URL", "postgresql://trader:trader@localhost:5544/trader"
)
_TEST_DB_URL = _BASE_DB_URL.rstrip("/") + "_test"
os.environ["TRADER_DATABASE_URL"] = _TEST_DB_URL


def _ensure_test_database() -> None:
    import psycopg

    try:
        psycopg.connect(_TEST_DB_URL, connect_timeout=5).close()
    except psycopg.OperationalError:
        name = _TEST_DB_URL.rsplit("/", 1)[1]
        with psycopg.connect(_BASE_DB_URL, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')


_ensure_test_database()

# CLI tests parse captured stdout as JSON; keep in-process app logs (structlog + stdlib)
# on stderr so they never interleave with a surface's output.
structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
logging.basicConfig(stream=sys.stderr)

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

# risk_state is a singleton row, not a collection: restore it to the migration's
# defaults rather than truncating it away (a missing row is fail-closed by design).
RESET_RISK_STATE = """
    UPDATE risk_state SET
        trading_paused = false, pause_reason = NULL, drift_detected = false,
        halted_reason = NULL, halted_at = NULL, day_session = NULL,
        day_start_equity = NULL, week_high_equity = NULL, updated_at = now()
    WHERE id = 1
"""


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
            await database.execute(RESET_RISK_STATE)
        finally:
            await database.close()

    asyncio.run(_truncate())


@pytest.fixture
async def db(_migrated: None) -> AsyncIterator[Database]:
    """A fresh pool per test (so it lives on this test's event loop), tables truncated."""
    database = Database(min_size=1, max_size=2)
    await database.open()
    await database.execute(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
    await database.execute(RESET_RISK_STATE)
    yield database
    await database.close()
