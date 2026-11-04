"""Polymarket producer (#142): crowd-priced odds on macro events, market-wide.

Polymarket's public Gamma API (``gamma-api.polymarket.com``) needs no key. Per topic
(``TRADER_POLYMARKET_TOPICS``: Fed decisions, CPI, recession by default) the producer
keeps the most-traded open markets that resolve in the future, with each one's first
outcome and its price as the implied probability. One row serves every symbol (the
``polymarket`` Source) and, behind ``TRADER_POLYMARKET_IN_MACRO_VIEW``, the Macro View
compiler's inputs.

Odds carry no historical vintage here, so a row is available from its fetch on: replay
before the first fetch sees none (``trader.loop.pit``). The approach follows
TradingAgents' ``dataflows/vendors/polymarket.py`` (Apache-2.0); our own implementation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import structlog

from trader.core.symbols import Symbol
from trader.loop.vendor_sources import POLYMARKET

log = structlog.get_logger(__name__)

SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
DEFAULT_TOPICS = ("Fed decision", "CPI", "recession")
MARKETS_PER_TOPIC = 3
POLL_SECONDS = 3_600.0


def _json_list(value: Any) -> list[Any]:
    """Gamma encodes ``outcomes`` / ``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _end(market: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(market.get("endDate")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _market(market: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """A forward-looking market as the row keeps it; None for a resolved or unpriced one.
    ``closed`` is the resolved flag (``active`` stays true after settlement)."""
    end = _end(market)
    if market.get("closed") or end is None or end < now:
        return None
    outcomes, prices = _json_list(market.get("outcomes")), _json_list(market.get("outcomePrices"))
    try:
        probability = float(prices[0])
    except (IndexError, TypeError, ValueError):
        return None
    change = market.get("oneWeekPriceChange")
    return {
        "question": str(market.get("question") or "").strip(),
        "outcome": str(outcomes[0]) if outcomes else "Yes",
        "probability": probability,
        "volume_usd": float(market.get("volumeNum") or 0),
        "end_date": end.date().isoformat(),
        "one_week_change": float(change) if isinstance(change, int | float) else None,
    }


class PolymarketProducer:
    name = POLYMARKET
    market_wide = True
    poll_seconds = POLL_SECONDS

    def __init__(self, http: httpx.AsyncClient, *, topics: list[str] | tuple[str, ...]) -> None:
        self._http = http
        self._topics = [t.strip() for t in topics if t.strip()]

    def serves(self, symbol: Symbol | None) -> bool:
        return True

    async def fetch(self, symbol: Symbol | None, *, now: datetime) -> dict[str, Any] | None:
        topics: list[dict[str, Any]] = []
        for topic in self._topics:
            try:
                resp = await self._http.get(SEARCH_URL, params={"q": topic, "limit_per_type": 20})
                resp.raise_for_status()
                events = resp.json().get("events") or []
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("polymarket_topic_unavailable", topic=topic, error=str(exc))
                continue
            markets = [
                kept
                for event in events
                for m in event.get("markets") or []
                if (kept := _market(m, now)) is not None and kept["question"]
            ]
            markets.sort(key=lambda m: m["volume_usd"], reverse=True)
            if markets:
                topics.append({"topic": topic, "markets": markets[:MARKETS_PER_TOPIC]})
        return {"topics": topics} if topics else None
