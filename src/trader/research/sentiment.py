"""X sentiment via xAI server-side ``x_search``, adapted from v1's poller.

The model searches X *server-side* on ``/v1/responses`` — the trader never calls X
directly. The load-bearing honesty rule: **zero citations with search filters active
means the answer came from model priors, not live X** — that read is journaled as
``unavailable`` and never carries a number (see
docs/lessons.md#sentiment-no-citations-is-unavailable). Every wire call is metered under the
``sentiment`` role (M2 discipline), and a per-day call cap bounds spend.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from trader.core.symbols import Symbol
from trader.llm.base import Completion, Usage
from trader.llm.usage import (
    price_per_million,
    x_search_tool_cost_usd,
    x_search_units_from_citations,
)
from trader.store.db import Database
from trader.store.repos import SentimentRepo

log = structlog.get_logger(__name__)

ROLE = "sentiment"
DEFAULT_DAILY_CALL_CAP = 24
DEFAULT_LOOKBACK_HOURS = 24

PROMPT = """Search X for the current sentiment on {name} ({ticker}).

Weigh only what the search returns — posts, volume, and who is posting. Then reply with
ONLY this JSON object, nothing else:
{{"score": <float -1..1, bearish..bullish>, "volume": "<quiet|normal|elevated|storm>",
"drivers": ["<short phrase>", ...]}}"""

JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class SentimentVerdict(BaseModel):
    score: float = Field(ge=-1.0, le=1.0)
    volume: str = Field(pattern="^(quiet|normal|elevated|storm)$")
    drivers: list[str] = []


class XSentimentClient:
    """Thin ``/v1/responses`` caller with the x_search tool attached."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        model: str,
        allowed_x_handles: list[str] | None = None,
    ) -> None:
        self._http = http
        self.model = model
        self._handles = allowed_x_handles or []

    async def search(
        self, prompt: str, *, from_date: str, to_date: str
    ) -> tuple[str, list[str], Usage]:
        """One server-side search. Returns (text, citations, usage)."""
        tool: dict[str, Any] = {"type": "x_search", "from_date": from_date, "to_date": to_date}
        if self._handles:
            tool["allowed_x_handles"] = self._handles
        resp = await self._http.post(
            "/responses",
            json={
                "model": self.model,
                "input": [{"role": "user", "content": prompt}],
                "tools": [tool],
                "store": False,
            },
        )
        resp.raise_for_status()
        payload = resp.json()

        texts: list[str] = []
        citations: list[str] = []
        for item in payload.get("output", []) or []:
            for chunk in item.get("content", []) or []:
                if chunk.get("type") in ("output_text", "text"):
                    texts.append(chunk.get("text", ""))
                    for note in chunk.get("annotations", []) or []:
                        if note.get("url"):
                            citations.append(note["url"])
        for url in payload.get("citations", []) or []:
            if url not in citations:
                citations.append(url)

        usage = payload.get("usage", {}) or {}
        return (
            "\n".join(texts),
            citations,
            Usage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
            ),
        )


class SentimentService:
    """Poll, honesty-check, persist, meter. The single door to X sentiment."""

    def __init__(
        self,
        db: Database,
        client: XSentimentClient | None,
        *,
        daily_call_cap: int = DEFAULT_DAILY_CALL_CAP,
        meter: Any | None = None,  # llm.usage.UsageMeter; built from db when omitted
        profiles: Any | None = None,  # llm.profiles.ProfileDesk; its sentiment Chain wins
    ) -> None:
        from trader.llm.usage import UsageMeter
        from trader.store.repos import UsageRepo

        self._db = db
        self._client = client  # the .env xAI client; None without an xAI credential
        self._profiles = profiles
        self._repo = SentimentRepo(db)
        self._cap = daily_call_cap
        self._meter = meter or UsageMeter(UsageRepo(db))

    async def get_x_sentiment(
        self,
        symbol: Symbol,
        *,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(tz=UTC)
        clients = await self._chain()
        if not clients:
            # Nothing to ask: not a poll, so nothing is journaled or metered.
            return {
                "status": "unavailable",
                "symbol": symbol.ticker,
                "reason": "no sentiment model",
            }
        client = clients[0]
        if await self._repo.calls_today() >= self._cap:
            return await self._unavailable(symbol, "daily cap reached", client)

        prompt = PROMPT.format(name=symbol.ticker.split("/")[0], ticker=symbol.ticker)
        for i, client in enumerate(clients):
            try:
                text, citations, usage = await client.search(
                    prompt,
                    from_date=(now - timedelta(hours=lookback_hours)).date().isoformat(),
                    to_date=now.date().isoformat(),
                )
                break
            except httpx.HTTPError:
                if i == len(clients) - 1:
                    raise
                log.warning("sentiment_model_failed", model=client.model)
        await self._record_usage(usage, client, citations=citations, now=now)

        if not citations:
            # Zero citations with filters active = model-prior, not live X
            # (see docs/lessons.md#sentiment-no-citations-is-unavailable).
            return await self._unavailable(symbol, "degraded: no live citations", client)

        match = JSON_OBJECT.search(text)
        if match is None:
            return await self._unavailable(symbol, "unparseable verdict", client)
        try:
            verdict = SentimentVerdict.model_validate(json.loads(match.group(0)))
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("sentiment_verdict_invalid", symbol=symbol.ticker, error=str(exc))
            return await self._unavailable(symbol, "unparseable verdict", client)

        await self._repo.insert(
            symbol=symbol.ticker,
            status="ok",
            score=verdict.score,
            volume=verdict.volume,
            drivers=verdict.drivers,
            citations=citations,
            model=client.model,
        )
        return {
            "status": "ok",
            "symbol": symbol.ticker,
            "score": verdict.score,
            "volume": verdict.volume,
            "drivers": verdict.drivers,
            "citations": citations,
        }

    async def _chain(self) -> list[Any]:
        """The active Profile's sentiment Chain when it sets one (#107), else ``.env``."""
        routed = await self._profiles.route(ROLE) if self._profiles is not None else None
        if routed is not None:
            return routed[1]
        return [self._client] if self._client is not None else []

    async def _unavailable(self, symbol: Symbol, reason: str, client: Any) -> dict[str, Any]:
        await self._repo.insert(
            symbol=symbol.ticker,
            status="unavailable",
            model=client.model,
            reason=reason,
        )
        log.info("sentiment_unavailable", symbol=symbol.ticker, reason=reason)
        return {"status": "unavailable", "symbol": symbol.ticker, "reason": reason}

    async def _record_usage(
        self, usage: Usage, client: Any, *, citations: list[str], now: datetime
    ) -> None:
        if self._meter is None:
            return
        prompt_price, completion_price = price_per_million("xai", client.model)
        token_cost = (
            usage.prompt_tokens * prompt_price + usage.completion_tokens * completion_price
        ) / 1_000_000
        posts, profiles = x_search_units_from_citations(citations)
        cost = token_cost + x_search_tool_cost_usd(
            calls=1, posts=posts, profiles=profiles, when=now
        )
        await self._meter.record(
            ROLE,
            Completion(text="", usage=usage, cost_usd=cost, provider="xai", model=client.model),
        )
