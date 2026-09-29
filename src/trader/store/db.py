"""Database access — one async psycopg3 pool, four query helpers, no ORM.

``fetch_iter`` streams via a server-side cursor; large result sets must never go through
``fetch_all`` (v1 loaded 19 GiB in one call that way — see docs/lessons.md#no-market-data-storage).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from trader.ops.config import get_settings

Params = tuple[Any, ...] | dict[str, Any] | None


class Database:
    """Async connection pool with typed query helpers. One instance per process."""

    def __init__(self, dsn: str | None = None, *, min_size: int = 1, max_size: int = 4) -> None:
        self._pool = AsyncConnectionPool(
            dsn or get_settings().database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """A pooled connection wrapped in a transaction (commit on exit, rollback on error)."""
        async with self._pool.connection() as conn:
            yield conn

    async def execute(self, sql: str, params: Params = None) -> int:
        """Run a statement; returns the affected row count."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def fetch_one(self, sql: str, params: Params = None) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def fetch_all(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        """Fetch a full (small, bounded) result set. Use ``fetch_iter`` for anything large."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def fetch_iter(
        self, sql: str, params: Params = None, *, batch_size: int = 500
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream rows through a server-side cursor, ``batch_size`` rows per round trip."""
        async with self._pool.connection() as conn:
            async with conn.cursor(name="trader_stream", row_factory=dict_row) as cur:
                cur.itersize = batch_size
                await cur.execute(sql, params)
                async for row in cur:
                    yield row
