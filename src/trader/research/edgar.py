"""SEC EDGAR producer (#142): recent filings and as-filed fundamentals per US filer.

Free and keyless, but SEC refuses a caller that does not identify itself: every request
carries ``TRADER_SEC_EDGAR_USER_AGENT`` (a name and a contact address). Without one the
producer is not built and the ``sec_filings`` Source has nothing to read.

One fetch per symbol reads three public documents: the ticker map
(``www.sec.gov/files/company_tickers.json``, cached a day), the filer's submissions
(recent 8-K/10-Q/10-K with their acceptance times) and its XBRL company facts (every
reported value with the filing that reported it). The row keeps a digest, never the
documents: the latest filings, and a few line items' quarterly, annual and balance facts
from 10-Q/10-K filings over the last few years, every vintage kept, so a restatement
reads at the figure that was on file at the time.

Each item is dated by when it became public: a filing by its acceptance time, a fact by
the acceptance of the filing that reported it (``accn``), else by the end of its filing
day. EDGAR stamps ``acceptanceDateTime`` in New York time behind a ``Z``; it is read as
New York time, which is never earlier than the truth. The approach follows TradingAgents'
``dataflows/vendors/sec_edgar.py`` (Apache-2.0); this is our own implementation, and it is
a backup to the OpenBB SEC provider (#141), not a replacement.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog

from trader.core.symbols import AssetClass, Symbol
from trader.loop import pit
from trader.loop.vendor_sources import SEC_FILINGS

log = structlog.get_logger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

FORMS = frozenset({"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A"})
FACT_FORMS = frozenset({"10-Q", "10-Q/A", "10-K", "10-K/A"})
MAX_FILINGS = 20
FACT_YEARS = 3
TICKER_MAP_TTL_SECONDS = 86_400.0
POLL_SECONDS = 21_600.0  # filings change a few times a quarter; 6 h is plenty
# Line items, each with the tags filers use for it, best first. The first tag that
# reports wins and values are never summed across tags (that would double count).
ITEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Revenue",
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    ),
    ("Net Income", ("NetIncomeLoss",)),
    ("Diluted EPS", ("EarningsPerShareDiluted",)),
    ("Operating Cash Flow", ("NetCashProvidedByUsedInOperatingActivities",)),
    ("Total Assets", ("Assets",)),
    ("Total Liabilities", ("Liabilities",)),
)
_CONTACT = re.compile(r"\S+@\S+\.\S+")


def accepted_at(raw: str) -> datetime | None:
    """EDGAR's ``acceptanceDateTime`` as an aware UTC instant, read as New York time."""
    try:
        naive = datetime.fromisoformat(raw.replace("Z", "").split(".")[0])
    except (AttributeError, ValueError):
        return None
    return naive.replace(tzinfo=pit.NEW_YORK).astimezone(UTC)


class EdgarProducer:
    name = SEC_FILINGS
    market_wide = False
    poll_seconds = POLL_SECONDS

    def __init__(self, http: httpx.AsyncClient, *, user_agent: str) -> None:
        if not _CONTACT.search(user_agent or ""):
            raise ValueError(
                "SEC EDGAR needs a User-Agent with a contact address, e.g. "
                "'Salamander research ops@example.org' (TRADER_SEC_EDGAR_USER_AGENT)"
            )
        self._http = http
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._tickers: tuple[float, dict[str, str]] | None = None

    def serves(self, symbol: Symbol) -> bool:
        return symbol.asset_class == AssetClass.US_EQUITY

    async def _get(self, url: str) -> Any:
        resp = await self._http.get(url, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def cik_for(self, ticker: str) -> str | None:
        """The filer's zero-padded CIK, or None when the ticker files nothing with SEC."""
        if self._tickers is None or time.monotonic() - self._tickers[0] > TICKER_MAP_TTL_SECONDS:
            table = await self._get(TICKERS_URL)
            mapping = {
                str(e["ticker"]).upper(): f"{int(e['cik_str']):010d}" for e in table.values()
            }
            self._tickers = (time.monotonic(), mapping)
        # EDGAR spells class shares with a dash: BRK.B is BRK-B.
        return self._tickers[1].get(ticker.upper().replace(".", "-"))

    async def fetch(self, symbol: Symbol | None, *, now: datetime) -> dict[str, Any] | None:
        assert symbol is not None
        cik = await self.cik_for(symbol.ticker)
        if cik is None:
            log.info("edgar_not_a_filer", symbol=symbol.ticker)
            return None
        submissions = await self._get(SUBMISSIONS_URL.format(cik=cik))
        facts = await self._get(FACTS_URL.format(cik=cik))
        filings, acceptance = _filings(cik, submissions)
        return {
            "cik": cik,
            "name": submissions.get("name") or facts.get("entityName"),
            "filings": filings,
            "facts": _facts(facts, acceptance, since=(now - timedelta(days=365 * FACT_YEARS))),
        }


def _filings(cik: str, submissions: dict[str, Any]) -> tuple[list[dict[str, Any]], dict]:
    """(the recent 8-K/10-Q/10-K filings, every recent accession's acceptance time)."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    columns = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form")
    rows = zip(*(recent.get(c) or [] for c in columns), strict=False)
    items = recent.get("items") or []
    documents = recent.get("primaryDocument") or []
    acceptance: dict[str, datetime] = {}
    filings: list[dict[str, Any]] = []
    for i, (accession, filed, report, accepted_raw, form) in enumerate(rows):
        at = accepted_at(accepted_raw)
        if at is None:
            continue  # undated: replay could not prove when it was public
        acceptance[accession] = at
        if form not in FORMS or len(filings) >= MAX_FILINGS:
            continue
        filings.append(
            {
                "form": form,
                "accession": accession,
                "filed": filed,
                "report_date": report or "",
                "accepted_at": at.isoformat(),
                "items": items[i] if i < len(items) else "",
                "url": ARCHIVE_URL.format(
                    cik=int(cik),
                    accession=accession.replace("-", ""),
                    document=documents[i] if i < len(documents) else "",
                ),
            }
        )
    return filings, acceptance


def _facts(
    companyfacts: dict[str, Any], acceptance: dict[str, datetime], *, since: datetime
) -> list[dict[str, Any]]:
    us_gaap = (companyfacts.get("facts") or {}).get("us-gaap") or {}
    cutoff = since.date().isoformat()
    out: list[dict[str, Any]] = []
    for item, tags in ITEMS:
        for tag in tags:
            kept = [
                {
                    "item": item,
                    "unit": unit,
                    "start": fact.get("start"),
                    "end": fact["end"],
                    "val": fact["val"],
                    "form": fact["form"],
                    "filed": fact["filed"],
                    "available_at": _fact_available_at(fact, acceptance).isoformat(),
                }
                for unit, values in ((us_gaap.get(tag) or {}).get("units") or {}).items()
                for fact in values
                if fact.get("form") in FACT_FORMS and fact.get("end", "") >= cutoff
            ]
            if kept:
                out += kept
                break
    return out


def _fact_available_at(fact: dict[str, Any], acceptance: dict[str, datetime]) -> datetime:
    at = acceptance.get(fact.get("accn", ""))
    if at is not None:
        return at
    return pit.day_end(date.fromisoformat(fact["filed"])).astimezone(UTC)
