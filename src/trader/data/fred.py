"""FRED client — macro series (VIXCLS etc.) for context building.

Not a ``MarketData`` provider: macro series are daily observations, not tradable bars.
Free API key, no meaningful rate limit at our volume; responses are TTL-cached in-memory
because series update at most daily and context builds may ask on every tick.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import httpx

from trader.data.base import MarketDataError

_BASE_URL = "https://api.stlouisfed.org"

DEFAULT_TTL_SECONDS = 3600.0


class Fred:
    """Async FRED client with an in-memory TTL cache. ``client`` injectable for tests."""

    def __init__(
        self,
        api_key: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Fred requires FRED_API_KEY")
        self._api_key = api_key
        self._ttl = ttl_seconds
        self._client = client or httpx.AsyncClient(base_url=_BASE_URL, timeout=10.0)
        self._cache: dict[tuple[str, int, date | None], tuple[float, list[tuple[date, float]]]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def get_series(
        self,
        series_id: str,
        limit: int = 30,
        *,
        observation_end: date | None = None,
    ) -> list[tuple[date, float]]:
        """The most recent ``limit`` observations, ascending ``(date, value)``.

        Missing observations (FRED encodes them as ``"."``) are dropped.
        ``observation_end`` caps the window (inclusive) so a replay step can ask for
        the prints that existed that day instead of whatever is newest at call time.
        The TTL cache keeps the fetched window in memory only — nothing is stored.
        """
        key = (series_id, limit, observation_end)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < self._ttl:
            return cached[1]

        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        if observation_end is not None:
            params["observation_end"] = observation_end.isoformat()
        try:
            resp = await self._client.get("/fred/series/observations", params=params)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise MarketDataError(f"fred: {exc}") from exc

        observations = [
            (date.fromisoformat(row["date"]), float(row["value"]))
            for row in reversed(payload.get("observations", []))  # desc -> ascending
            if row.get("value") not in (None, ".")
        ]
        self._cache[key] = (now, observations)
        return observations
