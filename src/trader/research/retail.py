"""Retail sentiment producer (#142): StockTwits and Reddit chatter per symbol, as counts.

Both feeds are keyless. StockTwits' public symbol stream
(``api.stocktwits.com/api/2/streams/symbol/<SYM>.json``; crypto as ``BTC.X``) returns its
latest messages, each with an optional user-set Bullish/Bearish label; the row keeps the
count inside the last day and how many carry each label. Reddit's subreddit search feed
(``www.reddit.com/r/<sub>/search.rss``) gives the count of posts naming the ticker
inside the last week. No message text is stored and no model scores anything: counts
first, so a lane built on four posts reads as four posts.

This is its own Source (``retail_sentiment``), beside X sentiment and never merged into
it. A feed that fails is recorded as unavailable; both failing records nothing. Counts
have no historical vintage, so a row is available from its fetch on
(``trader.loop.pit``). Feed choice follows TradingAgents' StockTwits/Reddit fallbacks
(Apache-2.0); our own implementation.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

from trader.core.symbols import AssetClass, Symbol
from trader.loop.vendor_sources import RETAIL_SENTIMENT

log = structlog.get_logger(__name__)

STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
REDDIT_URL = "https://www.reddit.com/r/{sub}/search.rss"
#: Reddit blocks anonymous clients; it asks for a descriptive User-Agent.
USER_AGENT = "salamander-trader/1 (research counts; +https://github.com/sudoTomas/trader)"
STOCKTWITS_WINDOW_HOURS = 24
REDDIT_WINDOW_DAYS = 7
#: StockTwits serves one page of this many messages: a full page inside the window means
#: there were at least that many.
STOCKTWITS_PAGE = 30
SUBREDDITS = {
    AssetClass.CRYPTO: ("CryptoCurrency",),
    AssetClass.US_EQUITY: ("wallstreetbets", "stocks", "investing"),
}
DEFAULT_SUBREDDITS = ("stocks",)
POLL_SECONDS = 3_600.0
_ATOM = "{http://www.w3.org/2005/Atom}"


def _instant(raw: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _base(symbol: Symbol) -> str:
    return symbol.ticker.split("/", 1)[0].upper()


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


class RetailSentimentProducer:
    name = RETAIL_SENTIMENT
    market_wide = False
    poll_seconds = POLL_SECONDS

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._headers = {"User-Agent": USER_AGENT}

    def serves(self, symbol: Symbol | None) -> bool:
        return symbol is not None

    async def fetch(self, symbol: Symbol | None, *, now: datetime) -> dict[str, Any] | None:
        assert symbol is not None
        stocktwits = await self._stocktwits(symbol, now)
        reddit = await self._reddit(symbol, now)
        if stocktwits["status"] != "ok" and reddit["status"] != "ok":
            log.info("retail_sentiment_unavailable", symbol=symbol.ticker)
            return None
        return {"stocktwits": stocktwits, "reddit": reddit}

    async def _stocktwits(self, symbol: Symbol, now: datetime) -> dict[str, Any]:
        spelled = f"{_base(symbol)}.X" if symbol.asset_class == AssetClass.CRYPTO else _base(symbol)
        try:
            resp = await self._http.get(
                STOCKTWITS_URL.format(symbol=spelled), headers=self._headers
            )
            resp.raise_for_status()
            messages = resp.json().get("messages") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("stocktwits_unavailable", symbol=symbol.ticker, error=str(exc))
            return _unavailable(f"stocktwits {type(exc).__name__}")
        since = now - timedelta(hours=STOCKTWITS_WINDOW_HOURS)
        window = [
            m
            for m in messages
            if (at := _instant(m.get("created_at"))) is not None and since <= at <= now
        ]
        labels = [((m.get("entities") or {}).get("sentiment") or {}).get("basic") for m in window]
        return {
            "status": "ok",
            "messages": len(window),
            "bullish": labels.count("Bullish"),
            "bearish": labels.count("Bearish"),
            "window_hours": STOCKTWITS_WINDOW_HOURS,
            "capped": len(window) >= STOCKTWITS_PAGE,
        }

    async def _reddit(self, symbol: Symbol, now: datetime) -> dict[str, Any]:
        subs = SUBREDDITS.get(symbol.asset_class, DEFAULT_SUBREDDITS)
        since = now - timedelta(days=REDDIT_WINDOW_DAYS)
        posts = 0
        read: list[str] = []
        for sub in subs:
            params = {"q": _base(symbol), "restrict_sr": "on", "sort": "new", "t": "week"}
            try:
                resp = await self._http.get(
                    REDDIT_URL.format(sub=sub), params=params, headers=self._headers
                )
                resp.raise_for_status()
                feed = ET.fromstring(resp.content)
            except (httpx.HTTPError, ET.ParseError) as exc:
                log.warning("reddit_unavailable", sub=sub, symbol=symbol.ticker, error=str(exc))
                continue
            read.append(sub)
            for entry in feed.iter(f"{_ATOM}entry"):
                stamp = entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated")
                at = _instant(stamp)
                if at is not None and since <= at <= now:
                    posts += 1
        if not read:
            return _unavailable("reddit unreachable")
        return {
            "status": "ok",
            "posts": posts,
            "window_days": REDDIT_WINDOW_DAYS,
            "subreddits": read,
        }
