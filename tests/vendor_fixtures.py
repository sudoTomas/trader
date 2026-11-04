"""Vendor fixtures (#142): stored rows for the vendor Sources, and recorded-shape HTTP
bodies for their producers (SEC EDGAR, Polymarket's Gamma API, StockTwits, Reddit RSS).

Every set plants something dated after ``NOW`` (the replay step the tests pin to), marked
so a test can assert it never reaches a prompt: ``FUTURE``, ``10-Q/A``, ``424.24B``, ``999``.
No test reaches the internet: producers run over ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx

from tests.test_loop import NOW
from trader.core.symbols import default_registry

AAPL = default_registry(["AAPL"]).get("AAPL")

# --- stored rows (what the producers wrote) ---------------------------------------------

SEC_PAYLOAD: dict[str, Any] = {
    "cik": "0000320193",
    "name": "Apple Inc.",
    "filings": [
        {
            "form": "10-Q",
            "accession": "0000320193-26-000070",
            "filed": "2026-07-31",
            "report_date": "2026-06-27",
            "accepted_at": "2026-07-31T20:30:12+00:00",
            "items": "",
        },
        {
            "form": "8-K",
            "accession": "0000320193-26-000080",
            "filed": "2026-08-11",
            "report_date": "",
            "accepted_at": "2026-08-11T20:05:00+00:00",
            "items": "2.02,9.01",
        },
        {  # accepted two hours after the step: never in a 15:00 prompt
            "form": "10-Q/A",
            "accession": "0000320193-26-000090",
            "filed": "2026-08-12",
            "report_date": "2026-06-27",
            "accepted_at": "2026-08-12T17:00:00+00:00",
            "items": "",
        },
    ],
    "facts": [
        {
            "item": "Revenue",
            "unit": "USD",
            "start": "2026-03-29",
            "end": "2026-06-27",
            "val": 94_000_000_000,
            "form": "10-Q",
            "filed": "2026-07-31",
            "available_at": "2026-07-31T20:30:12+00:00",
        },
        {  # the same quarter restated by the 10-Q/A: the vintage after the step
            "item": "Revenue",
            "unit": "USD",
            "start": "2026-03-29",
            "end": "2026-06-27",
            "val": 424_242_000_000,
            "form": "10-Q/A",
            "filed": "2026-08-12",
            "available_at": "2026-08-12T17:00:00+00:00",
        },
        {
            "item": "Revenue",
            "unit": "USD",
            "start": "2025-12-28",
            "end": "2026-03-28",
            "val": 90_000_000_000,
            "form": "10-Q",
            "filed": "2026-05-01",
            "available_at": "2026-05-01T20:30:00+00:00",
        },
        {
            "item": "Total Assets",
            "unit": "USD",
            "start": None,
            "end": "2026-06-27",
            "val": 350_000_000_000,
            "form": "10-Q",
            "filed": "2026-07-31",
            "available_at": "2026-07-31T20:30:12+00:00",
        },
    ],
}

POLY_NOW = {
    "topics": [
        {
            "topic": "Fed decision",
            "markets": [
                {
                    "question": "Will the Fed cut rates in September?",
                    "outcome": "Yes",
                    "probability": 0.62,
                    "volume_usd": 2_500_000,
                    "end_date": "2026-09-17",
                    "one_week_change": 0.04,
                }
            ],
        }
    ]
}
POLY_LATER = {
    "topics": [
        {
            "topic": "Fed decision",
            "markets": [
                {
                    "question": "FUTURE odds?",
                    "outcome": "Yes",
                    "probability": 0.9,
                    "volume_usd": 1,
                    "end_date": "2026-09-17",
                    "one_week_change": None,
                }
            ],
        }
    ]
}
RETAIL_NOW = {
    "stocktwits": {
        "status": "ok",
        "messages": 30,
        "bullish": 12,
        "bearish": 5,
        "window_hours": 24,
        "capped": True,
    },
    "reddit": {
        "status": "ok",
        "posts": 4,
        "window_days": 7,
        "subreddits": ["CryptoCurrency"],
    },
}
RETAIL_LATER = {
    "stocktwits": {
        "status": "ok",
        "messages": 999,
        "bullish": 999,
        "bearish": 0,
        "window_hours": 24,
        "capped": False,
    }
}

VENDOR_ROWS: list[dict[str, Any]] = [
    # Fetched three hours after the step: its dated items still serve the past.
    {"id": 7, "ts": NOW + timedelta(hours=3), "source": "sec_filings", "symbol": "AAPL",
     "payload": SEC_PAYLOAD},
    {"id": 3, "ts": NOW - timedelta(hours=2), "source": "polymarket", "symbol": None,
     "payload": POLY_NOW},
    {"id": 4, "ts": NOW + timedelta(hours=1), "source": "polymarket", "symbol": None,
     "payload": POLY_LATER},
    {"id": 5, "ts": NOW - timedelta(hours=1), "source": "retail_sentiment", "symbol": "BTC/USD",
     "payload": RETAIL_NOW},
    {"id": 6, "ts": NOW + timedelta(hours=2), "source": "retail_sentiment", "symbol": "BTC/USD",
     "payload": RETAIL_LATER},
]  # fmt: skip


class StubVendorReads:
    """VendorReadsRepo-shaped over fixed rows. ``latest`` returns the newest row however
    late it was fetched (as the live store would after the fact): the Sources' own as-of
    reads, and the point-in-time guard, are what keep later rows out of a replay."""

    def __init__(self, rows: list[dict[str, Any]] = VENDOR_ROWS) -> None:
        self._rows = list(rows)

    def _match(self, source: str, symbol: str | None) -> list[dict[str, Any]]:
        rows = [r for r in self._rows if r["source"] == source and r["symbol"] == symbol]
        return sorted(rows, key=lambda r: (r["ts"], r["id"]), reverse=True)

    async def latest(self, source: str, symbol: str | None) -> dict[str, Any] | None:
        rows = self._match(source, symbol)
        return rows[0] if rows else None

    async def recent(self, source: str, symbol: str | None, limit: int = 50) -> list[dict]:
        return self._match(source, symbol)[:limit]


# --- HTTP bodies (the vendors' wire shapes) ---------------------------------------------

EDGAR_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}
EDGAR_SUBMISSIONS = {
    "cik": "320193",
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000320193-26-000090",
                "0000320193-26-000085",
                "0000320193-26-000080",
                "0000320193-26-000070",
            ],
            "filingDate": ["2026-08-12", "2026-08-10", "2026-08-11", "2026-07-31"],
            "reportDate": ["2026-06-27", "", "", "2026-06-27"],
            # EDGAR stamps acceptance in New York time behind a "Z"; 13:00 here is 17:00 UTC.
            "acceptanceDateTime": [
                "2026-08-12T13:00:00.000Z",
                "2026-08-10T18:00:00.000Z",
                "2026-08-11T16:05:00.000Z",
                "2026-07-31T16:30:12.000Z",
            ],
            "form": ["10-Q/A", "4", "8-K", "10-Q"],
            "items": ["", "", "2.02,9.01", ""],
            "primaryDocument": ["a.htm", "b.xml", "c.htm", "d.htm"],
        }
    },
}
EDGAR_FACTS = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "USD": [
                        {"start": "2026-03-29", "end": "2026-06-27", "val": 94000000000,
                         "accn": "0000320193-26-000070", "form": "10-Q", "filed": "2026-07-31"},
                        {"start": "2026-03-29", "end": "2026-06-27", "val": 424242000000,
                         "accn": "0000320193-26-000090", "form": "10-Q/A", "filed": "2026-08-12"},
                        # Not in the recent filings list: dated by its filing day's end.
                        {"start": "2025-12-28", "end": "2026-03-28", "val": 90000000000,
                         "accn": "0000320193-26-000040", "form": "10-Q", "filed": "2026-05-01"},
                        # Older than the producer keeps.
                        {"start": "2019-12-29", "end": "2020-03-28", "val": 58000000000,
                         "accn": "0000320193-20-000010", "form": "10-Q", "filed": "2020-05-01"},
                    ]
                }
            },
            "Revenues": {  # a second revenue tag: first match wins, never summed
                "units": {"USD": [{"start": "2026-03-29", "end": "2026-06-27", "val": 1,
                                   "accn": "0000320193-26-000070", "form": "10-Q",
                                   "filed": "2026-07-31"}]}
            },
            "Assets": {
                "units": {"USD": [{"end": "2026-06-27", "val": 350000000000,
                                   "accn": "0000320193-26-000070", "form": "10-Q",
                                   "filed": "2026-07-31"}]}
            },
            "EarningsPerShareDiluted": {
                "units": {"USD/shares": [{"start": "2026-03-29", "end": "2026-06-27",
                                          "val": 1.57, "accn": "0000320193-26-000070",
                                          "form": "10-Q", "filed": "2026-07-31"}]}
            },
            "Liabilities": {  # an 8-K exhibit's figure: not a 10-Q/10-K fact, skipped
                "units": {"USD": [{"end": "2026-06-27", "val": 5, "accn": "x",
                                   "form": "8-K", "filed": "2026-08-11"}]}
            },
        }
    },
}  # fmt: skip

GAMMA_SEARCH = {
    "events": [
        {
            "title": "Fed decision in September",
            "markets": [
                {
                    "question": "Will the Fed cut rates in September?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.62", "0.38"]',
                    "volumeNum": 2500000,
                    "endDate": "2026-09-17T00:00:00Z",
                    "closed": False,
                    "oneWeekPriceChange": 0.04,
                },
                {
                    "question": "Will the Fed hike in September?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.03", "0.97"]',
                    "volumeNum": 900000,
                    "endDate": "2026-09-17T00:00:00Z",
                    "closed": False,
                },
                {  # resolved: not a forward-looking signal
                    "question": "Fed cut in July?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0", "1"]',
                    "volumeNum": 9000000,
                    "endDate": "2026-07-29T00:00:00Z",
                    "closed": True,
                },
                {  # past its end date though not yet marked closed
                    "question": "Fed cut in June?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.5", "0.5"]',
                    "volumeNum": 8000000,
                    "endDate": "2026-06-17T00:00:00Z",
                    "closed": False,
                },
            ],
        }
    ]
}


def stocktwits_stream() -> dict[str, Any]:
    """Five messages in the last day (3 bullish, 1 bearish, 1 unlabeled), one two days old,
    and one stamped after the clock."""

    def msg(i: int, hours_ago: float, label: str | None) -> dict[str, Any]:
        stamp = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "id": i,
            "body": "&#39;to the moon&#39;",
            "created_at": stamp,
            "entities": {"sentiment": {"basic": label} if label else None},
        }

    return {
        "symbol": {"symbol": "BTC.X"},
        "messages": [
            msg(9, -1, "Bullish"),  # after the clock
            msg(1, 1, "Bullish"),
            msg(2, 2, "Bullish"),
            msg(3, 3, "Bearish"),
            msg(4, 5, None),
            msg(5, 20, "Bullish"),
            msg(6, 48, "Bearish"),
        ],
    }


def reddit_atom(*hours_ago: float) -> str:
    entries = "".join(
        "<entry><title>post</title>"
        f"<updated>{(NOW - timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M:%S+00:00')}</updated>"
        "</entry>"
        for h in hours_ago
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'


def transport(routes: dict[str, Any], seen: list[httpx.Request] | None = None):
    """A MockTransport answering by URL path prefix (host + path); a callable route gets
    the request. Anything unrouted is a 404, so no test can reach a real vendor."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        key = f"{request.url.host}{request.url.path}"
        for prefix, body in routes.items():
            if key.startswith(prefix):
                if callable(body):
                    return body(request)
                if isinstance(body, httpx.Response):
                    return body
                if isinstance(body, str):
                    return httpx.Response(200, text=body)
                return httpx.Response(200, content=json.dumps(body).encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler)
