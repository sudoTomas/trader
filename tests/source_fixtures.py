"""In-memory Source inputs shared by the Source contract and Bot #1 prompt-parity tests.

Repo-shaped stubs (``current`` / ``get`` / ``latest`` / ``recent``) over fixed rows, so
the parity snapshots in ``tests/golden/bot_one_prompts.json`` are reproducible without a
database. The snapshots were taken on the pre-Sources ContextBuilder (constructor flags);
the parity test rebuilds every scenario through Sources and compares byte for byte.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from tests.test_loop import NOW
from trader.core.symbols import default_registry
from trader.loop.scheduler import Trigger

BTC = default_registry().get("BTC/USD")
SPY = default_registry().get("SPY")

MACRO_VIEWS: list[dict[str, Any]] = [
    {
        "version": 1,
        "created_at": NOW - timedelta(days=3),
        "global_thesis": "Liquidity is tightening.",
        "sections": {"crypto": "BTC funding is rich.", "us_equity": "Breadth is narrow."},
    },
    {
        "version": 2,
        "created_at": NOW - timedelta(hours=12),
        "global_thesis": "Liquidity is easing; risk-on.",
        "sections": {"crypto": "ETF inflows steady.\nBasis normal.", "us_equity": "Rotation."},
    },
    {
        "version": 3,
        "created_at": NOW + timedelta(hours=1),  # compiled after the clock; replay must not see it
        "global_thesis": "Later view.",
        "sections": {"crypto": "Later crypto."},
    },
]

BRIEFS: list[dict[str, Any]] = [
    {
        "version": 1,
        "ts": NOW - timedelta(days=2),
        "symbol": "BTC/USD",
        "thesis": "Range trade.",
        "points": ["Support 58k"],
    },
    {"version": 2, "ts": NOW - timedelta(days=1), "symbol": "SPY", "thesis": "Grind up."},
    {
        "version": 3,
        "ts": NOW - timedelta(hours=6),
        "symbol": "BTC/USD",
        "thesis": "Breakout watch.",
        "points": ["Volume rising", "  ", "Resistance 62k"],
    },
    {
        "version": 4,
        "ts": NOW + timedelta(hours=2),
        "symbol": "BTC/USD",
        "thesis": "Later brief.",
        "points": [],
    },
]

SENTIMENT: list[dict[str, Any]] = [
    {
        "id": 10,
        "symbol": "BTC/USD",
        "status": "ok",
        "score": -0.6,
        "volume": "elevated",
        "ts": NOW - timedelta(days=2),
    },
    {
        "id": 11,
        "symbol": "BTC/USD",
        "status": "error",
        "score": None,
        "volume": None,
        "ts": NOW - timedelta(hours=3),
    },
    {
        "id": 12,
        "symbol": "BTC/USD",
        "status": "ok",
        "score": 0.4,
        "volume": "storm",
        "ts": NOW - timedelta(hours=1),
    },
    {
        "id": 13,
        "symbol": "SPY",
        "status": "ok",
        "score": 0.1,
        "volume": None,
        "ts": NOW - timedelta(hours=2),
    },
]

FRED_PRINTS: list[tuple[date, float]] = [
    (NOW.date() - timedelta(days=3), 16.0),
    (NOW.date() - timedelta(days=1), 17.5),
    (NOW.date(), 18.2),
    (NOW.date() + timedelta(days=1), 424242.0),  # after the clock; never in a prompt
]

HELD = {"qty": 0.5, "avg_entry_price": 150.0, "updated_at": NOW - timedelta(days=3, hours=4)}
RECENT = [
    {"ts": (NOW - timedelta(hours=1)).isoformat(), "trigger": "heartbeat", "status": "shadow",
     "decision": {"action": "hold"}},
    {"ts": (NOW - timedelta(hours=2)).isoformat(), "trigger": "vol_spike", "status": "denied",
     "decision": {"action": "open"}},
]  # fmt: skip


class StubMacroViews:
    """MacroViewsRepo-shaped. ``current`` is the newest row that exists by the clock."""

    def __init__(self, rows: list[dict[str, Any]] = MACRO_VIEWS, *, now: datetime = NOW) -> None:
        self._rows = [r for r in rows if r["created_at"] <= now] if now else list(rows)
        self._all = {r["version"]: r for r in rows}

    async def current(self) -> dict[str, Any] | None:
        return max(self._rows, key=lambda r: r["version"]) if self._rows else None

    async def get(self, version: int) -> dict[str, Any] | None:
        return self._all.get(version)


class StubBriefs:
    """BriefsRepo-shaped; versions are one global identity across symbols."""

    def __init__(self, rows: list[dict[str, Any]] = BRIEFS, *, now: datetime = NOW) -> None:
        self._rows = [r for r in rows if r["ts"] <= now]
        self._all = {r["version"]: r for r in rows}

    async def current(self, symbol: str) -> dict[str, Any] | None:
        rows = [r for r in self._rows if r["symbol"] == symbol]
        return max(rows, key=lambda r: r["version"]) if rows else None

    async def get(self, version: int) -> dict[str, Any] | None:
        return self._all.get(version)


class StubSentiment:
    """SentimentRepo-shaped: ``latest`` for live, ``recent`` for the pinned path."""

    def __init__(self, rows: list[dict[str, Any]] = SENTIMENT, *, now: datetime = NOW) -> None:
        self._rows = [r for r in rows if r["ts"] <= now]
        self._all = list(rows)

    async def latest(self, symbol: str, *, status: str = "ok") -> dict[str, Any] | None:
        rows = [r for r in self._rows if r["symbol"] == symbol and r["status"] == status]
        return max(rows, key=lambda r: (r["ts"], r["id"])) if rows else None

    async def recent(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self._all if symbol is None or r["symbol"] == symbol]
        return sorted(rows, key=lambda r: (r["ts"], r["id"]), reverse=True)[:limit]


class StubFred:
    """data.fred.Fred-shaped; ignores ``observation_end`` on purpose (defense in depth)."""

    def __init__(self, series: list[tuple[date, float]] = FRED_PRINTS) -> None:
        self._series = series
        self.calls: list[dict[str, Any]] = []

    async def get_series(
        self, series_id: str, limit: int = 30, *, observation_end: date | None = None
    ) -> list[tuple[date, float]]:
        self.calls.append({"series_id": series_id, "observation_end": observation_end})
        return list(self._series)


# Bot #1's prompt-parity scenarios: each names the lanes the pre-Sources build had on
# (macro_view, briefs, exit_review — FRED is always on) and the build() inputs.
PARITY_SCENARIOS: dict[str, dict[str, Any]] = {
    "live_default_lanes": {
        "symbol": BTC,
        "lanes": {"macro_view": True, "briefs": False, "exit_review": False},
        "now": NOW,
        "as_of": None,
        "position_qty": 0.0,
        "position": None,
        "recent": RECENT,
    },
    "live_every_lane_held": {
        "symbol": BTC,
        "lanes": {"macro_view": True, "briefs": True, "exit_review": True},
        "now": NOW,
        "as_of": None,
        "position_qty": 0.5,
        "position": HELD,
        "recent": RECENT,
    },
    "live_equity_flat": {
        "symbol": SPY,
        "lanes": {"macro_view": True, "briefs": True, "exit_review": True},
        "now": NOW,
        "as_of": None,
        "position_qty": 0.0,
        "position": None,
        "recent": [],
    },
    "replay_pinned_every_lane": {
        "symbol": BTC,
        "lanes": {"macro_view": True, "briefs": True, "exit_review": True},
        "now": NOW - timedelta(days=1),
        "as_of": NOW - timedelta(days=1),
        "position_qty": -0.5,
        "position": {**HELD, "qty": -0.5, "opened_at": NOW - timedelta(days=1, hours=5)},
        "recent": [],
    },
    "replay_pinned_no_macro": {
        "symbol": BTC,
        "lanes": {"macro_view": False, "briefs": True, "exit_review": False},
        "now": NOW - timedelta(hours=4),
        "as_of": NOW - timedelta(hours=4),
        "position_qty": 0.0,
        "position": None,
        "recent": [],
    },
}


def parity_triggers(symbol) -> list[Trigger]:
    return [Trigger("heartbeat", symbol.ticker, {"forced": True})]
