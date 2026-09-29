"""Twelve Data adapter — fallback provider for equities/ETFs.

Async adaptation of v1's working ``adapters/market_data/twelve_data.py``,
keeping its hardened contracts:

- Twelve Data reports request errors as HTTP 200 with ``{"status": "error", ...}`` —
  surfaced as ``MarketDataError`` so the composite falls through.
- ``/time_series`` returns newest-first; reversed here to ascending. Gap-honest: no
  synthetic bars over closed sessions.
- Requests pass ``timezone=UTC`` so datetime strings parse unambiguously.

Free tier: 8 credits/min, 800/day; quote and time_series cost 1 credit each
(budget documented in .env.example). No bid/ask book — quotes carry the last close on
both sides.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from trader.core.symbols import Symbol
from trader.core.types import Bar, Quote
from trader.data.base import MarketDataError

_BASE_URL = "https://api.twelvedata.com"

#: internal interval -> Twelve Data interval
_INTERVALS = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "1d": "1day"}

PROVIDER = "twelve_data"


def _parse_ts(raw: str) -> datetime:
    # Intraday: "2026-08-11 15:30:00" (UTC wall clock, per timezone=UTC).
    # Daily: bare "2026-08-11", labeled at UTC midnight of the trading date.
    return datetime.fromisoformat(raw).replace(tzinfo=UTC)


class TwelveData:
    """Async Twelve Data client. ``client`` is injectable for recorded-fixture tests."""

    name = PROVIDER

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("TwelveData requires TWELVE_DATA_API_KEY")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(
            base_url=_BASE_URL, headers={"Accept": "application/json"}, timeout=10.0
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.get(path, params={**params, "apikey": self._api_key})
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise MarketDataError(f"twelve_data: {exc}") from exc
        if isinstance(data, dict) and data.get("status") == "error":
            raise MarketDataError(f"twelve_data error {data.get('code')}: {data.get('message')}")
        return data

    async def get_bars(self, symbol: Symbol, interval: str, lookback: int) -> list[Bar]:
        try:
            td_interval = _INTERVALS[interval]
        except KeyError:
            raise MarketDataError(f"twelve_data: unsupported interval '{interval}'") from None
        payload = await self._get(
            "/time_series",
            {
                "symbol": symbol.for_provider(PROVIDER),
                "interval": td_interval,
                "outputsize": lookback,
                "timezone": "UTC",
            },
        )
        return [
            Bar(
                symbol=symbol.ticker,
                interval=interval,
                ts=_parse_ts(row["datetime"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
            )
            for row in reversed(payload.get("values", []))  # newest-first -> ascending
        ]

    async def get_latest_quote(self, symbol: Symbol) -> Quote:
        payload = await self._get("/quote", {"symbol": symbol.for_provider(PROVIDER)})
        price = float(payload["close"])
        raw_ts = payload.get("timestamp")
        ts = (
            datetime.fromtimestamp(float(raw_ts), tz=UTC)
            if raw_ts is not None
            else datetime.now(tz=UTC)
        )
        # No book access on this plan: last close quoted on both sides.
        return Quote(symbol=symbol.ticker, ts=ts, bid=price, ask=price)
