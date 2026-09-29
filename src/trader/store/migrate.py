"""Migration runner. A failed migration ABORTS the run — rollback, raise, nothing after it
is applied. (v1's runner silently continued past failures and left half-applied schemas;
see docs/lessons.md#migration-runner-aborts.)
"""

from __future__ import annotations

from pathlib import Path

import structlog

from trader.store.db import Database

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

log = structlog.get_logger(__name__)


class MigrationError(RuntimeError):
    """A migration failed; the schema is exactly as it was before that file."""


def _load_migrations(migrations_dir: Path) -> list[tuple[str, str]]:
    """Read every ``*.sql`` in filename order (sync — files are tiny, read before the loop)."""
    return [(p.name, p.read_text()) for p in sorted(migrations_dir.glob("*.sql"))]


async def run_migrations(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending ``*.sql`` in filename order. Returns the names applied."""
    migrations = _load_migrations(migrations_dir)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    done = {row["name"] for row in await db.fetch_all("SELECT name FROM schema_migrations")}

    applied: list[str] = []
    for name, sql in migrations:
        if name in done:
            continue
        try:
            # One transaction per file: the DDL and the bookkeeping row commit together.
            async with db.connection() as conn:
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
        except Exception as exc:
            raise MigrationError(f"migration {name} failed: {exc}") from exc
        log.info("migration_applied", name=name)
        applied.append(name)
    return applied
