"""Crypto derivatives via ccxt public endpoints (#144) — no keys.

Per venue (Binance, Bybit, OKX) for a BTC or ETH USDT perpetual: the current funding
rate, open interest and its 24h change, and the basis of the perp's mark over the
venue's index. Fetch-on-tick: the readings are used for one decide and journaled with
it, never stored as market data. A venue that fails drops out of the reading; all of
them failing is an error the Source turns into no reading.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from statistics import fmean
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_VENUES = ("binance", "bybit", "okx")
#: The bases this reads; a perp exists for both on every default venue.
BASES = frozenset({"BTC", "ETH"})
TIMEOUT_MS = 10_000
OI_HISTORY_BARS = 25  # 1h points: now and 24h ago


class DerivativesError(RuntimeError):
    """No venue answered."""


@dataclass(frozen=True)
class VenueReading:
    venue: str
    funding_rate: float | None
    open_interest_usd: float | None
    oi_change_24h: float | None
    basis: float | None  # perp mark / index - 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "funding_rate": self.funding_rate,
            "open_interest_usd": self.open_interest_usd,
            "oi_change_24h": self.oi_change_24h,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class DerivativesSnapshot:
    """Every venue that answered, and the cross-venue means the scorer ranks."""

    base: str
    venues: tuple[VenueReading, ...]

    def _mean(self, name: str) -> float | None:
        values = [getattr(v, name) for v in self.venues if getattr(v, name) is not None]
        return fmean(values) if values else None

    @property
    def funding_rate(self) -> float | None:
        return self._mean("funding_rate")

    @property
    def oi_change_24h(self) -> float | None:
        return self._mean("oi_change_24h")

    @property
    def basis(self) -> float | None:
        return self._mean("basis")

    def as_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "funding_rate": self.funding_rate,
            "oi_change_24h": self.oi_change_24h,
            "basis": self.basis,
            "venues": [v.as_dict() for v in self.venues],
        }


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _ccxt_exchange(venue: str) -> Any:
    import ccxt.async_support as ccxt  # heavy import; only a subscribed Bot pays it

    return getattr(ccxt, venue)({"enableRateLimit": True, "timeout": TIMEOUT_MS})


class DerivativesClient:
    """Reads funding, open interest and basis from each venue. ``exchange_factory``
    (venue name -> a ccxt-shaped async exchange) is injectable for tests."""

    def __init__(
        self,
        venues: Iterable[str] = DEFAULT_VENUES,
        *,
        exchange_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.venues = tuple(venues)
        self._factory = exchange_factory or _ccxt_exchange
        self._exchanges: dict[str, Any] = {}

    def _exchange(self, venue: str) -> Any:
        if venue not in self._exchanges:
            self._exchanges[venue] = self._factory(venue)
        return self._exchanges[venue]

    async def close(self) -> None:
        for exchange in self._exchanges.values():
            await exchange.close()
        self._exchanges.clear()

    async def _venue(self, venue: str, base: str) -> VenueReading | None:
        market = f"{base}/USDT:USDT"
        exchange = self._exchange(venue)
        try:
            funding = await exchange.fetch_funding_rate(market)
        except Exception as exc:  # noqa: BLE001 - one venue down drops that venue only
            log.warning("derivatives_venue_unavailable", venue=venue, base=base, error=str(exc))
            return None
        mark = _num(funding.get("markPrice"))
        index = _num(funding.get("indexPrice"))
        basis = mark / index - 1.0 if mark and index else None

        oi_usd: float | None = None
        try:
            oi = await exchange.fetch_open_interest(market)
            oi_usd = _num(oi.get("openInterestValue"))
            amount = _num(oi.get("openInterestAmount"))
            if oi_usd is None and amount is not None and mark:
                oi_usd = amount * mark
        except Exception as exc:  # noqa: BLE001
            log.warning("derivatives_oi_unavailable", venue=venue, base=base, error=str(exc))

        oi_change: float | None = None
        try:
            history = await exchange.fetch_open_interest_history(
                market, "1h", limit=OI_HISTORY_BARS
            )
            points = [
                _num(h.get("openInterestValue")) or _num(h.get("openInterestAmount"))
                for h in sorted(history, key=lambda h: h.get("timestamp") or 0)
            ]
            points = [p for p in points if p]
            if len(points) >= 2:
                oi_change = points[-1] / points[0] - 1.0
        except Exception as exc:  # noqa: BLE001
            log.warning("derivatives_oi_history_unavailable", venue=venue, error=str(exc))

        return VenueReading(
            venue=venue,
            funding_rate=_num(funding.get("fundingRate")),
            open_interest_usd=oi_usd,
            oi_change_24h=oi_change,
            basis=basis,
        )

    async def snapshot(self, base: str) -> DerivativesSnapshot:
        if base not in BASES:
            raise DerivativesError(f"no perp reading for {base}")
        readings = await asyncio.gather(*(self._venue(v, base) for v in self.venues))
        venues = tuple(r for r in readings if r is not None)
        if not venues:
            raise DerivativesError(f"no venue answered for {base}")
        return DerivativesSnapshot(base=base, venues=venues)
