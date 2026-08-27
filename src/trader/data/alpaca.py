"""Alpaca Data adapter — primary provider for crypto AND US equities/ETFs.

Same API keys as the broker. Free (Basic) tier: crypto data is free; equities are the
IEX feed only, and historical requests are budgeted at ~200 req/min (documented in
.env.example). Fetch-on-tick: nothing is persisted here.

Endpoints (https://data.alpaca.markets):
- crypto:   /v1beta3/crypto/us/bars, /v1beta3/crypto/us/latest/quotes
- equities: /v2/stocks/bars, /v2/stocks/quotes/latest  (feed=iex on the free tier)

Bars are requested newest-first (``sort=desc``) so ``limit`` bounds the *most recent*
window, then reversed to the ascending order consumers expect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from trader.core.symbols import AssetClass, Symbol
from trader.core.types import Bar, Quote
from trader.data.base import MarketDataError

_BASE_URL = "https://data.alpaca.markets"

#: internal interval -> Alpaca timeframe
_TIMEFRAMES = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day"}
_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}

PROVIDER = "alpaca_data"


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class AlpacaData:
    """Async Alpaca Data client. ``client`` is injectable for recorded-fixture tests."""

    name = PROVIDER

    def __init__(
        self, key_id: str, secret: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        if not (key_id and secret):
            raise ValueError("AlpacaData requires ALPACA_API_KEY_ID and ALPACA_API_SECRET")
        self._client = client or httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
            timeout=10.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise MarketDataError(f"alpaca_data: {exc}") from exc

    async def get_bars(self, symbol: Symbol, interval: str, lookback: int) -> list[Bar]:
        try:
            timeframe = _TIMEFRAMES[interval]
        except KeyError:
            raise MarketDataError(f"alpaca_data: unsupported interval '{interval}'") from None
        spelled = symbol.for_provider(PROVIDER)
        # Explicit `start`: without it Alpaca defaults to the beginning of the CURRENT
        # day, silently capping any lookback to today's bars (a 100-bar hourly request
        # came back as 15 bars in production — caught by EAI-188's first live replay).
        # 5x slack covers closed sessions; limit + sort=desc still bounds the response
        # to exactly the most recent `lookback` bars.
        window = timedelta(seconds=_INTERVAL_SECONDS[interval] * lookback * 5)
        params: dict[str, Any] = {
            "symbols": spelled,
            "timeframe": timeframe,
            "start": (datetime.now(tz=UTC) - window).isoformat(),
            "limit": lookback,
            "sort": "desc",
        }
        if symbol.asset_class is AssetClass.CRYPTO:
            path = "/v1beta3/crypto/us/bars"
        else:
            path = "/v2/stocks/bars"
            params["feed"] = "iex"  # free-tier feed
            params["adjustment"] = "split"
        payload = await self._get(path, params)
        rows = (payload.get("bars") or {}).get(spelled, [])
        # Alpaca caps one response (~210 rows observed) no matter what `limit` says and
        # paginates via next_page_token; ignoring it silently truncates deep lookbacks
        # to the most recent page (found by EAI-190's gate replay — 6 days of a 2-week
        # backfill came back missing). Pages continue oldest-ward under sort=desc.
        pages = 1
        while len(rows) < lookback and payload.get("next_page_token") and pages < 50:
            payload = await self._get(path, {**params, "page_token": payload["next_page_token"]})
            rows += (payload.get("bars") or {}).get(spelled, [])
            pages += 1
        rows = rows[:lookback]  # desc: the first `lookback` rows are the most recent
        return [
            Bar(
                symbol=symbol.ticker,
                interval=interval,
                ts=_parse_ts(row["t"]),
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row["v"]),
            )
            for row in reversed(rows)  # desc from the API -> ascending for consumers
        ]

    async def get_latest_quote(self, symbol: Symbol) -> Quote:
        spelled = symbol.for_provider(PROVIDER)
        if symbol.asset_class is AssetClass.CRYPTO:
            payload = await self._get("/v1beta3/crypto/us/latest/quotes", {"symbols": spelled})
        else:
            payload = await self._get(
                "/v2/stocks/quotes/latest", {"symbols": spelled, "feed": "iex"}
            )
        quote = (payload.get("quotes") or {}).get(spelled)
        if not quote:
            raise MarketDataError(f"alpaca_data: no quote for {symbol.ticker}")
        return Quote(
            symbol=symbol.ticker,
            ts=_parse_ts(quote["t"]),
            bid=float(quote["bp"]),
            ask=float(quote["ap"]),
        )
