"""Static screening universes shipped with the repo. Pure data, no I/O.

The screener (#5) ranks a bounded, known list rather than "the whole market": a static
S&P 100 keeps every cycle to one or two multi-symbol Alpaca requests and makes the
shortlist reproducible from the repo alone. Constituents drift a few names a year;
refresh the tuple by hand and bump the date in the comment.
"""

from __future__ import annotations

#: S&P 100 constituents. Source: https://en.wikipedia.org/wiki/S%26P_100 (components
#: table, "as of September 21, 2026"), fetched 2026-09-29. Tickers are spelled the way
#: Alpaca's stock endpoints expect (``BRK.B``, not ``BRK-B``). Both Alphabet share
#: classes are in the index, so the list has 101 entries.
SP100: tuple[str, ...] = (
    "AAPL",
    "ABBV",
    "ABT",
    "ACN",
    "ADBE",
    "AMAT",
    "AMD",
    "AMGN",
    "AMT",
    "AMZN",
    "ANET",
    "AVGO",
    "AXP",
    "BA",
    "BAC",
    "BKNG",
    "BLK",
    "BMY",
    "BNY",
    "BRK.B",
    "C",
    "CAT",
    "CMCSA",
    "COF",
    "COP",
    "COST",
    "CRM",
    "CSCO",
    "CVS",
    "CVX",
    "DE",
    "DELL",
    "DHR",
    "DIS",
    "DUK",
    "EMR",
    "FDX",
    "GD",
    "GE",
    "GEV",
    "GILD",
    "GM",
    "GOOG",
    "GOOGL",
    "GS",
    "HD",
    "IBM",
    "INTC",
    "INTU",
    "ISRG",
    "JNJ",
    "JPM",
    "KO",
    "LIN",
    "LLY",
    "LMT",
    "LOW",
    "LRCX",
    "MA",
    "MCD",
    "MDLZ",
    "MDT",
    "META",
    "MMM",
    "MO",
    "MRK",
    "MS",
    "MSFT",
    "MU",
    "NEE",
    "NFLX",
    "NOW",
    "NVDA",
    "ORCL",
    "PANW",
    "PEP",
    "PFE",
    "PG",
    "PLTR",
    "PM",
    "QCOM",
    "RTX",
    "SBUX",
    "SCHW",
    "SNDK",
    "SO",
    "T",
    "TMO",
    "TMUS",
    "TSLA",
    "TXN",
    "UBER",
    "UNH",
    "UNP",
    "UPS",
    "USB",
    "V",
    "VZ",
    "WFC",
    "WMT",
    "XOM",
)

_UNIVERSES: dict[str, tuple[str, ...]] = {"sp100": SP100}


def universe(name: str) -> tuple[str, ...]:
    """The named universe (case-insensitive). Unknown names raise ``KeyError`` so a
    surface can answer 422 instead of silently screening nothing."""
    try:
        return _UNIVERSES[name.strip().lower()]
    except KeyError:
        raise KeyError(f"unknown universe '{name}' (known: {sorted(_UNIVERSES)})") from None


__all__ = ["SP100", "universe"]
