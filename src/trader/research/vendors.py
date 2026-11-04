"""Vendor desk (#142): runs the vendor producers and writes what they return as
``vendor_reads`` rows, the versions the vendor Sources read.

A producer runs only for what some Bot subscribes to: each tick a Bot hands the desk its
Sources and symbols (``schedule``), and a vendor Source it does not name is never fetched.
Every producer has its own cadence (EDGAR 6 h, Polymarket and retail 1 h), one stamp per
(Source, symbol) shared by every Bot and seeded from the store after a restart. Fetches
run in one background task so a slow vendor never holds up a tick, and a failure is
logged, never raised: these are advisory inputs. The operator's ``collect_vendor``
fetches one now on every door.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import structlog

from trader.core.symbols import Symbol
from trader.loop.sources import Subscription, subscriptions
from trader.store.db import Database
from trader.store.repos import VendorReadsRepo

log = structlog.get_logger(__name__)

#: Per-request timeout: a vendor is advisory, it never gets to stall the desk for long.
TIMEOUT_SECONDS = 20.0
NO_EDGAR = (
    "SEC EDGAR is off in this build: set TRADER_SEC_EDGAR_USER_AGENT to a name and contact"
    " address (SEC refuses anonymous callers)"
)

Key = tuple[str, str | None]


class Producer(Protocol):
    name: str
    market_wide: bool
    poll_seconds: float

    def serves(self, symbol: Symbol) -> bool: ...

    async def fetch(self, symbol: Symbol | None, *, now: datetime) -> dict[str, Any] | None: ...


class VendorDesk:
    def __init__(self, db: Database, producers: Iterable[Producer]) -> None:
        self._repo = VendorReadsRepo(db)
        self.producers: dict[str, Producer] = {p.name: p for p in producers}
        self._stamps: dict[Key, datetime] = {}
        self._seeded: set[Key] = set()
        self._pending: dict[Key, tuple[Symbol | None, datetime]] = {}
        self._task: asyncio.Task | None = None

    async def collect(self, name: str, symbol: Symbol | None, *, now: datetime | None = None):
        """One fetch now: the row written, or why there is none."""
        now = now or datetime.now(tz=UTC)
        producer = self.producers.get(name)
        if producer is None:
            reason = NO_EDGAR if name == "sec_filings" else f"no producer for {name!r}"
            return {"status": "unavailable", "source": name, "reason": reason}
        target = None if producer.market_wide else symbol
        ticker = target.ticker if target is not None else None
        if not producer.market_wide and (symbol is None or not producer.serves(symbol)):
            reason = "needs a symbol" if symbol is None else f"does not cover {symbol.ticker}"
            return {"status": "unavailable", "source": name, "symbol": ticker, "reason": reason}
        self._stamps[(name, ticker)] = now
        try:
            payload = await producer.fetch(target, now=now)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("vendor_fetch_failed", source=name, symbol=ticker, error=str(exc))
            return {"status": "unavailable", "source": name, "symbol": ticker, "reason": str(exc)}
        if payload is None:
            return {"status": "unavailable", "source": name, "symbol": ticker, "reason": "no data"}
        row_id = await self._repo.add(source=name, symbol=ticker, payload=payload)
        log.info("vendor_read_recorded", source=name, symbol=ticker, id=row_id)
        return {"status": "ok", "source": name, "symbol": ticker, "id": row_id, "payload": payload}

    def wanted(
        self, spec: Iterable[Subscription | Mapping[str, Any]], symbols: Iterable[Symbol]
    ) -> dict[Key, Symbol | None]:
        """What these subscriptions ask the desk to keep fresh, keyed (Source, ticker)."""
        out: dict[Key, Symbol | None] = {}
        symbols = list(symbols)
        for sub in subscriptions(spec):
            producer = self.producers.get(sub.source)
            if producer is None:
                continue
            if producer.market_wide:
                out[(sub.source, None)] = None
                continue
            for symbol in symbols:
                if producer.serves(symbol):
                    out[(sub.source, symbol.ticker)] = symbol
        return out

    async def collect_due(
        self,
        spec: Iterable[Subscription | Mapping[str, Any]],
        symbols: Iterable[Symbol],
        *,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch whatever these subscriptions want that is past its cadence. Never raises."""
        results = []
        for key, symbol in self.wanted(spec, symbols).items():
            results += await self._collect_if_due(key, symbol, now)
        return results

    def schedule(
        self,
        spec: Iterable[Subscription | Mapping[str, Any]],
        symbols: Iterable[Symbol],
        *,
        now: datetime,
    ) -> None:
        """``collect_due`` in the background: queue the work, start the one drain task."""
        for key, symbol in self.wanted(spec, symbols).items():
            self._pending.setdefault(key, (symbol, now))
        if self._pending and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._drain(), name="vendor-desk")

    async def idle(self) -> None:
        """Wait for the background drain (tests, shutdown)."""
        if self._task is not None:
            await self._task

    async def _drain(self) -> None:
        while self._pending:
            key, (symbol, now) = self._pending.popitem()
            await self._collect_if_due(key, symbol, now)

    async def _collect_if_due(
        self, key: Key, symbol: Symbol | None, now: datetime
    ) -> list[dict[str, Any]]:
        try:
            last = await self._last(key)
            if (
                last is not None
                and (now - last).total_seconds() < self.producers[key[0]].poll_seconds
            ):
                return []
            return [await self.collect(key[0], symbol, now=now)]
        except Exception:  # noqa: BLE001 - advisory inputs never break the loop
            log.exception("vendor_collect_failed", source=key[0], symbol=key[1])
            return []

    async def _last(self, key: Key) -> datetime | None:
        if key not in self._seeded:
            self._seeded.add(key)
            row = await self._repo.latest(*key)
            if row is not None and key not in self._stamps:
                self._stamps[key] = row["ts"]
        return self._stamps.get(key)


def build_vendor_desk(
    db: Database, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None
) -> VendorDesk:
    """Every keyless producer, plus EDGAR when ``TRADER_SEC_EDGAR_USER_AGENT`` is set.
    Construction does no I/O; nothing runs until a Bot subscribes."""
    from trader.research.edgar import EdgarProducer
    from trader.research.polymarket import PolymarketProducer
    from trader.research.retail import RetailSentimentProducer

    http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=True)
    topics = [t for t in settings.polymarket_topics.split(",") if t.strip()]
    producers: list[Producer] = [
        PolymarketProducer(http, topics=topics),
        RetailSentimentProducer(http),
    ]
    if settings.sec_edgar_user_agent.strip():
        producers.append(EdgarProducer(http, user_agent=settings.sec_edgar_user_agent.strip()))
    return VendorDesk(db, producers)
