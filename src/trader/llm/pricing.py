"""Live layered pricing (#105): every model has a price, and local models cost nothing.

Per (Provider, model), first answer wins:

1. ``local``: a local Provider. $0 whatever the model, because it runs on hardware you
   own; the same open-weight id on a hosted Provider is paid.
2. ``override``: a manual price set on any surface (audited).
3. ``provider``: the Provider's own models list, when it carries prices (OpenRouter).
   xAI and Together are not used: their model lists carrying prices is unconfirmed.
4. ``catalog``: LiteLLM's public ``model_prices_and_context_window.json``, Provider-
   prefixed id first (``groq/…``), then the bare id.
5. ``unknown``: $0, and metering logs the gap once.

Remote documents are fetched at most once a day per URL; a failed fetch keeps the last
copy and is not retried until the day is up. Prices are USD per 1M tokens, as in
``llm/usage.py``.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from trader.store.db import Database

log = structlog.get_logger(__name__)

CATALOG_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
DAY_SECONDS = 86_400.0
FETCH_TIMEOUT_SECONDS = 10.0
#: The catalog's id prefix per preset. Absent: try the bare id only.
CATALOG_PREFIX = {
    "openai": "",
    "anthropic": "",
    "gemini": "gemini/",
    "xai": "xai/",
    "openrouter": "openrouter/",
    "groq": "groq/",
    "together": "together_ai/",
}
#: Presets whose own ``/models`` list carries per-token prices.
LISTS_PRICES = frozenset({"openrouter"})


def _price(price_in: float, price_out: float, source: str) -> dict[str, Any]:
    return {"price_in": price_in, "price_out": price_out, "source": source}


def _per_million(per_token: Any) -> float | None:
    try:
        value = float(per_token) * 1_000_000
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


class PriceBook:
    """The layered lookup. One per process; ProviderDesk owns it."""

    def __init__(
        self, db: Database, http: httpx.AsyncClient, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._db = db
        self._http = http
        self._clock = clock
        # url -> (last attempt, payload or None)
        self._docs: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def prices(self, row: dict[str, Any], models: list[str]) -> list[dict[str, Any]]:
        """The price of each of ``models`` on the Provider ``row``, in order."""
        if row["local"]:
            return [_price(0.0, 0.0, "local") for _ in models]
        overrides = {
            r["model"]: r
            for r in await self._db.fetch_all(
                "SELECT model, price_in, price_out FROM llm_price_overrides WHERE provider_id = %s",
                (row["id"],),
            )
        }
        out = []
        for model in models:
            if model in overrides:
                o = overrides[model]
                out.append(_price(float(o["price_in"]), float(o["price_out"]), "override"))
                continue
            found = await self._listed(row, model) or await self._catalogued(row, model)
            out.append(found or _price(0.0, 0.0, "unknown"))
        return out

    async def price(self, row: dict[str, Any], model: str) -> dict[str, Any]:
        return (await self.prices(row, [model]))[0]

    async def _listed(self, row: dict[str, Any], model: str) -> dict[str, Any] | None:
        if row["preset"] not in LISTS_PRICES:
            return None
        doc = await self._doc(row["base_url"].rstrip("/") + "/models")
        for item in (doc or {}).get("data", []) if isinstance(doc, dict) else []:
            if item.get("id") == model:
                pricing = item.get("pricing") or {}
                p_in = _per_million(pricing.get("prompt"))
                p_out = _per_million(pricing.get("completion"))
                if p_in is not None and p_out is not None:
                    return _price(p_in, p_out, "provider")
        return None

    async def _catalogued(self, row: dict[str, Any], model: str) -> dict[str, Any] | None:
        catalog = await self._doc(CATALOG_URL)
        if not isinstance(catalog, dict):
            return None
        prefix = CATALOG_PREFIX.get(row["preset"])
        ids = [model] if not prefix else [prefix + model, model]
        for key in ids:
            entry = catalog.get(key)
            if not isinstance(entry, dict):
                continue
            p_in = _per_million(entry.get("input_cost_per_token"))
            p_out = _per_million(entry.get("output_cost_per_token"))
            if p_in is not None and p_out is not None:
                return _price(p_in, p_out, "catalog")
        return None

    async def _doc(self, url: str) -> Any:
        async with self._lock:
            now = self._clock()
            cached = self._docs.get(url)
            if cached is not None and now - cached[0] < DAY_SECONDS:
                return cached[1]
            previous = cached[1] if cached is not None else None
            try:
                resp = await self._http.get(url, timeout=FETCH_TIMEOUT_SECONDS)
                if resp.status_code != 200:
                    raise ValueError(f"HTTP {resp.status_code}")
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                # Keep the last copy; try again tomorrow, not on every call.
                log.warning("price_source_unavailable", url=url, error=str(exc)[:200])
                payload = previous
            self._docs[url] = (now, payload)
            return payload
