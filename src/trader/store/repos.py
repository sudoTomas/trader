"""Repos — the only SQL surface above ``db.py``. One thin class per aggregate, typed
signatures, dict rows out, no ORM. Each repo carries only what its first consumer
(M1 data / M2 usage / M3 loop / M4 desk) needs; grow them with the milestone.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from psycopg.types.json import Jsonb

from trader.store.db import Database

Row = dict[str, Any]


class DecisionsRepo:
    """The journal (M3). Every wakeup writes one row, replay-ready."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(
        self,
        *,
        trigger: str,
        status: str,
        symbol: str | None = None,
        asset_class: str | None = None,
        prompt: str | None = None,
        raw_output: str | None = None,
        decision: dict[str, Any] | None = None,
        risk_verdicts: list[dict[str, Any]] | None = None,
        macro_view_version: int | None = None,
        brief_version: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO decisions (trigger, status, symbol, asset_class, prompt, raw_output,
                                   decision, risk_verdicts, macro_view_version, brief_version,
                                   cost_usd, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                trigger,
                status,
                symbol,
                asset_class,
                prompt,
                raw_output,
                Jsonb(decision) if decision is not None else None,
                Jsonb(risk_verdicts) if risk_verdicts is not None else None,
                macro_view_version,
                brief_version,
                cost_usd,
                latency_ms,
            ),
        )
        assert row is not None
        return row["id"]

    async def get(self, decision_id: int) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM decisions WHERE id = %s", (decision_id,))

    async def recent(self, limit: int = 50) -> list[Row]:
        return await self._db.fetch_all(
            "SELECT * FROM decisions ORDER BY ts DESC, id DESC LIMIT %s", (limit,)
        )


class OrdersRepo:
    """Orders + their fills (M3 executor). The order row is persisted BEFORE submit."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(
        self,
        *,
        client_order_id: str,
        symbol: str,
        asset_class: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
        order_type: str = "market",
        limit_price: float | None = None,
        time_in_force: str = "gtc",
        decision_id: int | None = None,
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO orders (client_order_id, symbol, asset_class, side, qty, notional,
                                order_type, limit_price, time_in_force, decision_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                client_order_id,
                symbol,
                asset_class,
                side,
                qty,
                notional,
                order_type,
                limit_price,
                time_in_force,
                decision_id,
            ),
        )
        assert row is not None
        return row["id"]

    async def get_by_client_order_id(self, client_order_id: str) -> Row | None:
        return await self._db.fetch_one(
            "SELECT * FROM orders WHERE client_order_id = %s", (client_order_id,)
        )

    async def set_status(
        self,
        order_id: int,
        status: str,
        *,
        broker_order_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE orders
            SET status = %s,
                broker_order_id = COALESCE(%s, broker_order_id),
                raw = COALESCE(%s, raw),
                updated_at = now()
            WHERE id = %s
            """,
            (status, broker_order_id, Jsonb(raw) if raw is not None else None, order_id),
        )

    async def add_fill(
        self,
        order_id: int,
        *,
        ts: datetime,
        qty: float,
        price: float,
        broker_fill_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO fills (order_id, ts, qty, price, broker_fill_id, raw)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (broker_fill_id) DO NOTHING
            RETURNING id
            """,
            (order_id, ts, qty, price, broker_fill_id, Jsonb(raw) if raw is not None else None),
        )
        return row["id"] if row else 0  # 0 = duplicate trade-updates replay, already recorded

    async def fills_for(self, order_id: int) -> list[Row]:
        return await self._db.fetch_all(
            "SELECT * FROM fills WHERE order_id = %s ORDER BY ts", (order_id,)
        )


class PositionsRepo:
    """Snapshot mirror of broker positions (M3 reconcile). Fills are the ledger, not this."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(
        self,
        *,
        symbol: str,
        asset_class: str,
        qty: float,
        avg_entry_price: float | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO positions (symbol, asset_class, qty, avg_entry_price)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (symbol) DO UPDATE
            SET qty = EXCLUDED.qty,
                avg_entry_price = EXCLUDED.avg_entry_price,
                updated_at = now()
            """,
            (symbol, asset_class, qty, avg_entry_price),
        )

    async def get(self, symbol: str) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM positions WHERE symbol = %s", (symbol,))

    async def all(self) -> list[Row]:
        return await self._db.fetch_all("SELECT * FROM positions ORDER BY symbol")


class ResearchNotesRepo:
    """Research Desk notes (M4). Content is immutable; only lifecycle fields change."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        *,
        title: str,
        body: str,
        tags: list[str] | None = None,
        asset_classes: list[str] | None = None,
        symbols: list[str] | None = None,
        source: str | None = None,
        pinned: bool = False,
        expires_at: datetime | None = None,
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO research_notes (title, body, tags, asset_classes, symbols, source,
                                        pinned, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                title,
                body,
                tags or [],
                asset_classes or [],
                symbols or [],
                source,
                pinned,
                expires_at,
            ),
        )
        assert row is not None
        return row["id"]

    async def get(self, note_id: int) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM research_notes WHERE id = %s", (note_id,))

    async def list_current(self) -> list[Row]:
        """Notes the compiler should see: active/disputed and not auto-staled out."""
        return await self.list_notes()

    async def list_notes(
        self,
        *,
        include_stale: bool = False,
        include_retired: bool = False,
        limit: int = 100,
    ) -> list[Row]:
        """The compiler's view by default; the flags widen it for operator listings."""
        return await self._db.fetch_all(
            """
            SELECT * FROM research_notes
            WHERE (%s OR status IN ('active', 'disputed'))
              AND (%s OR pinned OR expires_at IS NULL OR expires_at > now())
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (include_retired, include_stale, limit),
        )

    async def set_status(self, note_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE research_notes SET status = %s WHERE id = %s", (status, note_id)
        )

    async def retire(self, note_id: int) -> bool:
        """Retire once: False when the note is missing or already retired."""
        n = await self._db.execute(
            "UPDATE research_notes SET status = 'retired' WHERE id = %s AND status <> 'retired'",
            (note_id,),
        )
        return n == 1


class MacroViewsRepo:
    """Versioned compiled digests (M4). New compile = new row; version is the identity."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        *,
        compiled_by: str,
        global_thesis: str,
        sections: dict[str, Any],
        note_ids: list[int],
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO macro_views (compiled_by, global_thesis, sections, note_ids)
            VALUES (%s, %s, %s, %s)
            RETURNING version
            """,
            (compiled_by, global_thesis, Jsonb(sections), note_ids),
        )
        assert row is not None
        return row["version"]

    async def current(self) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM macro_views ORDER BY version DESC LIMIT 1")

    async def get(self, version: int) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM macro_views WHERE version = %s", (version,))


class BriefsRepo:
    """Per-candidate research briefs (M4, EAI-192). New compile = new row; version is a
    single global identity so ``decisions.brief_version`` points at exactly one row."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        *,
        symbol: str,
        compiled_by: str,
        thesis: str,
        points: list[str],
        note_ids: list[int],
        sentiment_id: int | None = None,
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO briefs (symbol, compiled_by, thesis, points, note_ids, sentiment_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING version
            """,
            (symbol, compiled_by, thesis, Jsonb(points), note_ids, sentiment_id),
        )
        assert row is not None
        return row["version"]

    async def current(self, symbol: str) -> Row | None:
        return await self._db.fetch_one(
            "SELECT * FROM briefs WHERE symbol = %s ORDER BY version DESC LIMIT 1", (symbol,)
        )

    async def get(self, version: int) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM briefs WHERE version = %s", (version,))


class BarsRepo:
    """Immutable read-through candle cache (M1). Never a live-path dependency."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_many(self, bars: list[dict[str, Any]]) -> int:
        """Insert candles, ignoring ones already cached (immutable — never overwrite)."""
        count = 0
        async with self._db.connection() as conn:
            for b in bars:
                cur = await conn.execute(
                    """
                    INSERT INTO bars (symbol, interval, ts, open, high, low, close, volume)
                    VALUES (%(symbol)s, %(interval)s, %(ts)s, %(open)s, %(high)s, %(low)s,
                            %(close)s, %(volume)s)
                    ON CONFLICT (symbol, interval, ts) DO NOTHING
                    """,
                    b,
                )
                count += cur.rowcount
        return count

    async def range(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Row]:
        return await self._db.fetch_all(
            """
            SELECT * FROM bars
            WHERE symbol = %s AND interval = %s AND ts >= %s AND ts < %s
            ORDER BY ts
            """,
            (symbol, interval, start, end),
        )


class ApprovalsRepo:
    """HITL queue (M3). Large orders park here; a human resolves via any surface."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self, *, order_intent: dict[str, Any], reason: str, decision_id: int | None = None
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO approvals (order_intent, reason, decision_id)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (Jsonb(order_intent), reason, decision_id),
        )
        assert row is not None
        return row["id"]

    async def get(self, approval_id: int) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM approvals WHERE id = %s", (approval_id,))

    async def pending(self) -> list[Row]:
        return await self._db.fetch_all(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at"
        )

    async def resolve(self, approval_id: int, *, status: str, resolved_by: str) -> bool:
        """Resolve a pending approval. False if it wasn't pending (already resolved/expired)."""
        n = await self._db.execute(
            """
            UPDATE approvals
            SET status = %s, resolved_by = %s, resolved_at = now()
            WHERE id = %s AND status = 'pending'
            """,
            (status, resolved_by, approval_id),
        )
        return n == 1


class EvalRepo:
    """Replay eval runs + their steps (M5). Offline decide reruns journal here, never
    into ``decisions`` — the live journal stays evidence of what actually ran."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_run(
        self,
        *,
        label: str,
        symbol: str,
        interval: str,
        start_ts: datetime,
        end_ts: datetime,
        config: dict[str, Any] | None = None,
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO eval_runs (label, symbol, interval, start_ts, end_ts, config)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (label, symbol, interval, start_ts, end_ts, Jsonb(config or {})),
        )
        assert row is not None
        return row["id"]

    async def finish_run(self, run_id: int, *, status: str, summary: dict[str, Any]) -> None:
        await self._db.execute(
            "UPDATE eval_runs SET status = %s, summary = %s WHERE id = %s",
            (status, Jsonb(summary), run_id),
        )

    async def add_step(
        self,
        run_id: int,
        *,
        ts: datetime,
        status: str,
        price: float | None = None,
        prompt: str | None = None,
        raw_output: str | None = None,
        decision: dict[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO eval_steps (run_id, ts, status, price, prompt, raw_output, decision,
                                    latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                run_id,
                ts,
                status,
                price,
                prompt,
                raw_output,
                Jsonb(decision) if decision is not None else None,
                latency_ms,
            ),
        )
        assert row is not None
        return row["id"]

    async def get_run(self, run_id: int) -> Row | None:
        return await self._db.fetch_one("SELECT * FROM eval_runs WHERE id = %s", (run_id,))

    async def runs(self, limit: int = 50) -> list[Row]:
        return await self._db.fetch_all(
            "SELECT * FROM eval_runs ORDER BY created_at DESC, id DESC LIMIT %s", (limit,)
        )

    async def steps_summary(self, run_id: int) -> list[Row]:
        """Steps without the prompt/raw_output payloads — a listing, not a dump."""
        return await self._db.fetch_all(
            "SELECT id, ts, status, price, decision, latency_ms"
            " FROM eval_steps WHERE run_id = %s ORDER BY ts",
            (run_id,),
        )


class SentimentRepo:
    """X sentiment polls (M4). Every poll journals, scored or not; only 'ok' rows carry
    a number (enforced by a table constraint)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert(
        self,
        *,
        symbol: str,
        status: str,
        score: float | None = None,
        volume: str | None = None,
        drivers: list[str] | None = None,
        citations: list[str] | None = None,
        model: str | None = None,
        reason: str | None = None,
    ) -> int:
        row = await self._db.fetch_one(
            """
            INSERT INTO sentiment_scores (symbol, status, score, volume, drivers, citations,
                                          model, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                symbol,
                status,
                score,
                volume,
                Jsonb(drivers or []),
                Jsonb(citations or []),
                model,
                reason,
            ),
        )
        assert row is not None
        return row["id"]

    async def latest(self, symbol: str, *, status: str = "ok") -> Row | None:
        return await self._db.fetch_one(
            "SELECT * FROM sentiment_scores WHERE symbol = %s AND status = %s"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (symbol, status),
        )

    async def recent(self, symbol: str | None = None, limit: int = 50) -> list[Row]:
        if symbol is None:
            return await self._db.fetch_all(
                "SELECT * FROM sentiment_scores ORDER BY ts DESC, id DESC LIMIT %s", (limit,)
            )
        return await self._db.fetch_all(
            "SELECT * FROM sentiment_scores WHERE symbol = %s ORDER BY ts DESC, id DESC LIMIT %s",
            (symbol, limit),
        )

    async def calls_today(self) -> int:
        """Polls that reached the wire today (UTC) — the daily-cap counter."""
        row = await self._db.fetch_one(
            "SELECT count(*) AS n FROM sentiment_scores"
            " WHERE ts >= date_trunc('day', now()) AND coalesce(reason, '') <> 'daily cap reached'"
        )
        assert row is not None
        return row["n"]


class UsageRepo:
    """Daily LLM usage rollup (M2). Accumulates real token counts so budgets see real spend."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        day: date,
        role: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO usage_daily (day, role, provider, model, requests,
                                     prompt_tokens, completion_tokens, cost_usd)
            VALUES (%s, %s, %s, %s, 1, %s, %s, %s)
            ON CONFLICT (day, role, provider, model) DO UPDATE
            SET requests = usage_daily.requests + 1,
                prompt_tokens = usage_daily.prompt_tokens + EXCLUDED.prompt_tokens,
                completion_tokens = usage_daily.completion_tokens + EXCLUDED.completion_tokens,
                cost_usd = usage_daily.cost_usd + EXCLUDED.cost_usd
            """,
            (day, role, provider, model, prompt_tokens, completion_tokens, cost_usd),
        )

    async def for_day(self, day: date) -> list[Row]:
        return await self._db.fetch_all(
            "SELECT * FROM usage_daily WHERE day = %s ORDER BY role, provider, model", (day,)
        )
