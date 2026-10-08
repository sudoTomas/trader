"""Spend caps (#108): a daily USD limit per Role, apart from the active Profile.

The shared registry asks ``reached(role)`` before each call; at the cap the Role's paid
models are skipped for the rest of the UTC day and a free one later in its Chain serves.
Spend is ``usage_daily`` for the Role today, so it counts every model that served it.
The first hit of a Role each day is logged and audited. A cap never blocks a wakeup by
itself: with no free model left the Role fails as it would in an outage.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from trader.store.audit import CALLER, SURFACE, audit
from trader.store.db import Database

log = structlog.get_logger(__name__)

ROLES = ("decide", "brief", "compiler", "sentiment", "analyst", "critic", "confirm")
ESTIMATE_DAYS = 7


class CapError(ValueError):
    """A cap operation was refused; the message says why."""


def _today() -> date:
    return datetime.now(tz=UTC).date()


class SpendCaps:
    """Every cap operation, and the per-call check the registry makes."""

    def __init__(self, db: Database, *, defaults: dict[str, float] | None = None) -> None:
        self._db = db
        self._defaults = dict(defaults or {})
        # (role, day) pairs already reported, so a hit is audited once a day per process.
        self._reported: set[tuple[str, date]] = set()

    async def cap(self, role: str) -> float | None:
        row = await self._db.fetch_one(
            "SELECT daily_usd FROM llm_spend_caps WHERE role = %s", (role,)
        )
        if row is not None:
            return float(row["daily_usd"])
        return self._defaults.get(role)

    async def spent_today(self, role: str) -> float:
        row = await self._db.fetch_one(
            "SELECT COALESCE(sum(cost_usd), 0) AS usd FROM usage_daily"
            " WHERE day = %s AND role = %s",
            (_today(), role),
        )
        return float(row["usd"]) if row else 0.0

    async def reached(self, role: str) -> bool:
        cap = await self.cap(role)
        if cap is None:
            return False
        spent = await self.spent_today(role)
        if spent < cap:
            return False
        key = (role, _today())
        if key not in self._reported:
            self._reported.add(key)
            log.warning("spend_cap_reached", role=role, spent_usd=spent, cap_usd=cap)
            await audit(
                self._db,
                actor="system",
                category="caps",
                action="spend_cap_reached",
                subject=role,
                payload={"cap_usd": cap, "spent_usd": round(spent, 6)},
            )
        return True

    async def list(self) -> dict[str, dict[str, Any]]:
        rows = {
            r["role"]: float(r["daily_usd"])
            for r in await self._db.fetch_all("SELECT role, daily_usd FROM llm_spend_caps")
        }
        spent = {
            r["role"]: float(r["usd"])
            for r in await self._db.fetch_all(
                "SELECT role, sum(cost_usd) AS usd FROM usage_daily WHERE day = %s GROUP BY role",
                (_today(),),
            )
        }
        out: dict[str, dict[str, Any]] = {}
        for role in ROLES:
            if role in rows:
                cap, source = rows[role], "set"
            elif role in self._defaults:
                cap, source = self._defaults[role], "default"
            else:
                cap, source = None, None
            out[role] = {"cap_usd": cap, "source": source, "spent_today": spent.get(role, 0.0)}
        return out

    async def set(self, role: str, daily_usd: float) -> dict[str, dict[str, Any]]:
        self._check_role(role)
        if not (math.isfinite(daily_usd) and daily_usd >= 0):
            raise CapError("a cap is a non-negative number of USD per day")
        await self._db.execute(
            """
            INSERT INTO llm_spend_caps (role, daily_usd) VALUES (%s, %s)
            ON CONFLICT (role) DO UPDATE SET daily_usd = EXCLUDED.daily_usd, updated_at = now()
            """,
            (role, daily_usd),
        )
        await self._audit("set_spend_cap", role, {"daily_usd": daily_usd})
        return await self.list()

    async def clear(self, role: str) -> dict[str, dict[str, Any]]:
        """Back to the Role's default (uncapped when it has none)."""
        self._check_role(role)
        if await self._db.execute("DELETE FROM llm_spend_caps WHERE role = %s", (role,)):
            await self._audit("clear_spend_cap", role, {})
        return await self.list()

    async def estimate(self, role: str, price: tuple[float, float]) -> float:
        """USD a day at ``price`` (per 1M prompt, completion tokens) for the Role's average
        daily token volume over the last 7 days."""
        row = await self._db.fetch_one(
            """
            SELECT COALESCE(sum(prompt_tokens), 0) AS p, COALESCE(sum(completion_tokens), 0) AS c
            FROM usage_daily WHERE role = %s AND day > %s
            """,
            (role, _today() - timedelta(days=ESTIMATE_DAYS)),
        )
        prompt, completion = (int(row["p"]), int(row["c"])) if row else (0, 0)
        total = prompt * price[0] + completion * price[1]
        return total / 1_000_000 / ESTIMATE_DAYS

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in ROLES:
            raise CapError(f"unknown Role {role!r}; one of {sorted(ROLES)}")

    async def _audit(self, action: str, role: str, payload: dict[str, Any]) -> None:
        await audit(
            self._db,
            actor=CALLER.get(),
            category="caps",
            action=action,
            subject=role,
            payload={**payload, "surface": SURFACE.get()},
        )
