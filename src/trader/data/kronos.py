"""Kronos client — the forecast sidecar (#144).

Kronos-small (MIT, ``shiyu-coder/Kronos``) runs in its own GPU container
(``infra/kronos/``), never inside the trader process. Per call the trader sends the
symbol's last ~400 bars and gets back N independently sampled close paths. The paths are
kept apart on purpose: the library's ``sample_count`` averages them into one path, which
throws away the spread the band is scored on. The summary computed here is what the
journal records: the share of paths that end up, and the median, p10 and p90 forward
return at the horizon.

Fetch-on-use: bars go out, a summary comes back, nothing is stored. The sidecar being
down is a normal state; the Source that calls this drops its reading and the decide goes
on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import numpy as np
import structlog

from trader.core.types import Bar

log = structlog.get_logger(__name__)

#: Bars sent per forecast. Kronos-small's context is 512; 400 leaves it headroom.
KRONOS_LOOKBACK = 400
KRONOS_INTERVAL = "1h"
TIMEOUT_SECONDS = 15.0


class KronosError(RuntimeError):
    """The sidecar did not answer, or answered something unusable."""


@dataclass(frozen=True)
class KronosForecast:
    """One forecast's summary. Returns are fractions vs ``last_close`` at the horizon."""

    model: str
    revision: str
    interval: str
    horizon_bars: int
    paths: int
    last_bar_ts: datetime
    last_close: float
    share_up: float
    median: float
    p10: float
    p90: float
    inference_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "revision": self.revision,
            "interval": self.interval,
            "horizon_bars": self.horizon_bars,
            "paths": self.paths,
            "last_bar_ts": self.last_bar_ts.isoformat(),
            "last_close": self.last_close,
            "share_up": self.share_up,
            "median": self.median,
            "p10": self.p10,
            "p90": self.p90,
            "inference_ms": self.inference_ms,
        }


def summarize_paths(paths: list[list[float]], last_close: float) -> dict[str, float]:
    """Each path's return at its last step vs ``last_close``: the share that end above
    it, and the median, p10 and p90 of those returns."""
    if last_close <= 0:
        raise KronosError("last close must be positive")
    ends = np.array([path[-1] for path in paths], dtype=float) / last_close - 1.0
    return {
        "share_up": float(np.mean(ends > 0)),
        "median": float(np.median(ends)),
        "p10": float(np.percentile(ends, 10)),
        "p90": float(np.percentile(ends, 90)),
    }


class KronosClient:
    """Async client for the sidecar's ``POST /forecast``. ``client`` injectable for tests."""

    def __init__(
        self,
        base_url: str,
        *,
        paths: int,
        horizon_bars: int,
        timeout_seconds: float = TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.paths = paths
        self.horizon_bars = horizon_bars
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def forecast(
        self, symbol: str, bars: list[Bar], *, interval: str = KRONOS_INTERVAL
    ) -> KronosForecast:
        if not bars:
            raise KronosError("no bars to forecast from")
        body = {
            "symbol": symbol,
            "interval": interval,
            "horizon": self.horizon_bars,
            "paths": self.paths,
            "bars": [
                {
                    "ts": b.ts.isoformat(),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars
            ],
        }
        try:
            resp = await self._client.post("/forecast", json=body)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise KronosError(f"kronos sidecar: {exc}") from exc
        paths = payload.get("paths") or []
        if not paths or any(len(p) != self.horizon_bars for p in paths):
            raise KronosError(f"kronos sidecar returned {len(paths)} unusable paths")
        last = bars[-1]
        stats = summarize_paths(paths, last.close)
        forecast = KronosForecast(
            model=str(payload.get("model", "")),
            revision=str(payload.get("revision", "")),
            interval=interval,
            horizon_bars=self.horizon_bars,
            paths=len(paths),
            last_bar_ts=last.ts,
            last_close=last.close,
            inference_ms=int(payload.get("inference_ms") or 0),
            **stats,
        )
        log.info(
            "kronos_forecast",
            symbol=symbol,
            inference_ms=forecast.inference_ms,
            paths=forecast.paths,
            share_up=forecast.share_up,
        )
        return forecast
