"""Market data layer tests — recorded fixtures over httpx.MockTransport, no network."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from trader.core.symbols import AssetClass, default_registry, make_symbol
from trader.data.alpaca import AlpacaData
from trader.data.base import MarketDataError
from trader.data.composite import CompositeMarketData
from trader.data.fred import Fred
from trader.data.twelve_data import TwelveData

BTC = default_registry().get("BTC/USD")
SPY = make_symbol("SPY", AssetClass.US_EQUITY)


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")


# --- alpaca -----------------------------------------------------------------

# Recorded shape of GET /v1beta3/crypto/us/bars (sort=desc → newest first).
ALPACA_CRYPTO_BARS = {
    "bars": {
        "BTC/USD": [
            {"t": "2026-08-11T10:02:00Z", "o": 101, "h": 102, "l": 100, "c": 101.5, "v": 7},
            {"t": "2026-08-11T10:01:00Z", "o": 100, "h": 101, "l": 99, "c": 101, "v": 5},
        ]
    },
    "next_page_token": None,
}

ALPACA_CRYPTO_QUOTE = {
    "quotes": {"BTC/USD": {"t": "2026-08-11T10:02:03Z", "bp": 101.4, "ap": 101.6}}
}


async def test_alpaca_crypto_bars_ascending():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=ALPACA_CRYPTO_BARS)

    data = AlpacaData("k", "s", client=mock_client(handler))
    bars = await data.get_bars(BTC, "1m", 2)

    assert seen["path"] == "/v1beta3/crypto/us/bars"
    assert seen["params"]["symbols"] == "BTC/USD"
    assert seen["params"]["sort"] == "desc"
    assert [b.ts for b in bars] == sorted(b.ts for b in bars)  # ascending
    assert bars[-1].close == 101.5 and bars[-1].ts.tzinfo is not None
    assert all(b.symbol == "BTC/USD" and b.interval == "1m" for b in bars)


async def test_alpaca_bars_follow_pagination_until_lookback_is_met():
    """Alpaca caps a bars response (~210 rows) regardless of `limit` and hands back a
    next_page_token — a client that ignores it silently truncates any deep lookback to
    the most recent page (found by EAI-190's gate replay: 2-week backfills came back
    as 210 bars and the replay skipped 6 days as no-data)."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        if "page_token" not in params:
            return httpx.Response(
                200,
                json={
                    "bars": {
                        "BTC/USD": [
                            {"t": "2026-08-11T10:02:00Z", "o": 1, "h": 1, "l": 1, "c": 3, "v": 1},
                            {"t": "2026-08-11T10:01:00Z", "o": 1, "h": 1, "l": 1, "c": 2, "v": 1},
                        ]
                    },
                    "next_page_token": "TOKEN-1",
                },
            )
        return httpx.Response(
            200,
            json={
                "bars": {
                    "BTC/USD": [
                        {"t": "2026-08-11T10:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
                    ]
                },
                "next_page_token": None,
            },
        )

    data = AlpacaData("k", "s", client=mock_client(handler))
    bars = await data.get_bars(BTC, "1m", 3)

    assert len(calls) == 2
    assert calls[1]["page_token"] == "TOKEN-1"
    assert [b.close for b in bars] == [1, 2, 3]  # ascending, all three pages' bars


async def test_alpaca_equity_bars_use_sip_with_a_delayed_end():
    """Free-tier Alpaca serves CONSOLIDATED (sip) historical bars when the window ends
    >=15 min in the past. IEX omits whole thin-hours bars (measured 2026-09-08: 8 vs 16
    on the same window), so completed-bar quality is a sip+delayed-end away — for $0."""
    from datetime import UTC, datetime, timedelta

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"bars": {"SPY": []}})

    data = AlpacaData("k", "s", client=mock_client(handler))
    assert await data.get_bars(SPY, "1d", 10) == []
    assert seen["path"] == "/v2/stocks/bars"
    assert seen["params"]["feed"] == "sip"
    assert seen["params"]["adjustment"] == "split"
    end = datetime.fromisoformat(seen["params"]["end"])
    assert timedelta(minutes=15) <= datetime.now(tz=UTC) - end <= timedelta(minutes=30)


async def test_alpaca_crypto_bars_send_no_feed_and_no_end():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"bars": {"BTC/USD": []}})

    data = AlpacaData("k", "s", client=mock_client(handler))
    await data.get_bars(BTC, "1h", 10)
    assert "feed" not in seen["params"] and "end" not in seen["params"]


async def test_alpaca_equity_quotes_stay_on_iex():
    """Quotes are the real-time mark; free-tier sip quotes are not available — the
    delayed end applies to BARS only."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200, json={"quotes": {"SPY": {"t": "2026-09-08T10:02:03Z", "bp": 769, "ap": 770}}}
        )

    data = AlpacaData("k", "s", client=mock_client(handler))
    quote = await data.get_latest_quote(SPY)
    assert seen["params"]["feed"] == "iex"
    assert quote.mid == 769.5


async def test_alpaca_quote_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ALPACA_CRYPTO_QUOTE)

    data = AlpacaData("k", "s", client=mock_client(handler))
    quote = await data.get_latest_quote(BTC)
    assert (quote.bid, quote.ask) == (101.4, 101.6)
    assert quote.mid == pytest.approx(101.5)

    failing = AlpacaData("k", "s", client=mock_client(lambda r: httpx.Response(403)))
    with pytest.raises(MarketDataError, match="alpaca_data"):
        await failing.get_bars(BTC, "1m", 5)
    with pytest.raises(MarketDataError, match="unsupported interval"):
        await data.get_bars(BTC, "3m", 5)
    with pytest.raises(ValueError, match="requires"):
        AlpacaData("", "")


# --- twelve data ------------------------------------------------------------

TWELVE_DATA_SERIES = {
    "meta": {"symbol": "SPY", "interval": "1day"},
    "values": [  # newest first, as the API returns
        {
            "datetime": "2026-08-11",
            "open": "2",
            "high": "3",
            "low": "1",
            "close": "2.5",
            "volume": "200",
        },
        {
            "datetime": "2026-08-10",
            "open": "1",
            "high": "2",
            "low": "0.5",
            "close": "1.5",
            "volume": "100",
        },
    ],
    "status": "ok",
}


async def test_twelve_data_bars_ascending_and_utc():
    data = TwelveData(
        "key", client=mock_client(lambda r: httpx.Response(200, json=TWELVE_DATA_SERIES))
    )
    bars = await data.get_bars(SPY, "1d", 2)
    assert [b.ts for b in bars] == [
        datetime(2026, 8, 10, tzinfo=UTC),
        datetime(2026, 8, 11, tzinfo=UTC),
    ]
    assert bars[-1].close == 2.5


async def test_twelve_data_error_in_200_body():
    body = {"status": "error", "code": 429, "message": "credits exhausted"}
    data = TwelveData("key", client=mock_client(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(MarketDataError, match="credits exhausted"):
        await data.get_bars(SPY, "1d", 2)


# --- fred -------------------------------------------------------------------

FRED_OBSERVATIONS = {
    "observations": [  # sort_order=desc → newest first; "." = missing
        {"date": "2026-08-11", "value": "17.4"},
        {"date": "2026-08-10", "value": "."},
        {"date": "2026-08-07", "value": "16.9"},
    ]
}


async def test_fred_parses_skips_missing_and_caches():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=FRED_OBSERVATIONS)

    fred = Fred("key", client=mock_client(handler))
    series = await fred.get_series("VIXCLS", limit=3)
    assert series == [(date(2026, 8, 7), 16.9), (date(2026, 8, 11), 17.4)]  # ascending, "." gone
    assert await fred.get_series("VIXCLS", limit=3) == series
    assert calls["n"] == 1  # second hit served from TTL cache


# --- composite --------------------------------------------------------------


class _StubProvider:
    def __init__(self, name: str, bars=None, fail: bool = False) -> None:
        self.name = name
        self._bars = bars or []
        self._fail = fail
        self.calls = 0

    async def get_bars(self, symbol, interval, lookback):
        self.calls += 1
        if self._fail:
            raise MarketDataError(f"{self.name} down")
        return self._bars

    async def get_latest_quote(self, symbol):
        raise NotImplementedError


async def test_composite_falls_through_to_fallback():
    bar = json.loads('{"x": 1}')  # sentinel payload; provider return value passes through
    primary = _StubProvider("primary", fail=True)
    fallback = _StubProvider("fallback", bars=[bar])
    md = CompositeMarketData({AssetClass.US_EQUITY: [primary, fallback]})
    assert await md.get_bars(SPY, "1d", 5) == [bar]
    assert primary.calls == 1 and fallback.calls == 1


async def test_composite_exhausted_chain_raises():
    md = CompositeMarketData({AssetClass.US_EQUITY: [_StubProvider("a", fail=True)]})
    with pytest.raises(MarketDataError, match="all providers failed"):
        await md.get_bars(SPY, "1d", 5)


async def test_composite_unrouted_asset_class_fails_safe():
    md = CompositeMarketData({AssetClass.US_EQUITY: [_StubProvider("a")]})
    with pytest.raises(MarketDataError, match="no market data providers"):
        await md.get_bars(BTC, "1m", 5)


async def test_alpaca_bars_request_reaches_back_past_todays_midnight():
    """Without an explicit `start`, Alpaca defaults to the beginning of the current day
    and `limit` silently caps to today's bars — a 100-bar hourly lookback came back as
    15 bars in production (EAI-188 first live replay). `start` must cover the lookback
    with slack for closed sessions."""
    from datetime import UTC, datetime, timedelta

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=ALPACA_CRYPTO_BARS)

    data = AlpacaData("k", "s", client=mock_client(handler))
    await data.get_bars(BTC, "1h", 100)

    start = datetime.fromisoformat(seen["params"]["start"])
    assert start.tzinfo is not None
    assert start <= datetime.now(tz=UTC) - timedelta(hours=100)  # covers the lookback
