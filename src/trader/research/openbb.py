"""OpenBB producer (#141): earnings, options and fundamentals, written as Source rows.

A scheduled job (``refresh_openbb``; ``infra/systemd/trader-openbb-refresh.timer``), never
the decide tick. Each refresh asks the OpenBB sidecar (``trader.data.openbb``) for keyless
providers only, boils each answer down to a compact dated summary, and appends one
``openbb_snapshots`` row per (kind, symbol). The Sources in ``trader.loop.sources`` read
those rows; RiskGuard's earnings blackout reads the earnings row's report date.

- **earnings**: Nasdaq's calendar over the next ``EARNINGS_LOOKAHEAD_DAYS`` (one call for
  every symbol) gives the next report date, its time (before the open / after the close)
  and the consensus EPS; Nasdaq's EPS history gives the last surprise. A symbol with no
  report in the window still gets a row, so "no report soon" is a fresh fact, not a gap.
- **options**: Cboe's chain (Nasdaq's when Cboe is down): at the nearest expiry at least
  a day out, the ATM implied vol, the implied move (ATM straddle / spot) and the put/call
  skew (IV of the 95% put minus IV of the 105% call).
- **fundamentals**: Nasdaq's price-target consensus and ratios, SEC insider trades over
  ``INSIDER_WINDOW_DAYS``, FINRA's latest short interest. Any part may be missing.

Scraped endpoints break, so every provider has its own breaker: ``BREAKER_THRESHOLD``
failures in a row skip that provider for ``BREAKER_COOLDOWN``, and its kinds age toward
stale instead of hammering a dead page. A failed fetch writes nothing, so the last good
row stays the Source's answer and its age shows. An unreachable sidecar ends the refresh
without charging any provider.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from trader.data.openbb import OpenBBClient, OpenBBError, SidecarUnreachable
from trader.loop.sources import EARNINGS, FUNDAMENTALS, OPTIONS
from trader.store.db import Database
from trader.store.repos import OpenBBRepo

log = structlog.get_logger(__name__)

EARNINGS_LOOKAHEAD_DAYS = 45
INSIDER_WINDOW_DAYS = 90
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = timedelta(hours=6)
#: Moneyness of the skew legs: the put 5% below spot, the call 5% above.
SKEW_PUT, SKEW_CALL = 0.95, 1.05
#: Nasdaq's ``time`` codes, as the prompt says them.
REPORT_TIMES = {
    "pre-market": "before market open",
    "after-hours": "after market close",
}

ROUTES = {
    "calendar": "equity/calendar/earnings",
    "eps": "equity/fundamental/historical_eps",
    "chains": "derivatives/options/chains",
    "price_target": "equity/estimates/consensus",
    "ratios": "equity/fundamental/ratios",
    "insider": "equity/ownership/insider_trading",
    "short_interest": "equity/shorts/short_interest",
}


# --- small readers ------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _for(rows: Iterable[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """Rows about ``symbol`` (rows without a symbol are taken as the query's own)."""
    return [r for r in rows if str(r.get("symbol") or symbol).upper() == symbol.upper()]


# --- summaries (pure: recorded fixtures in, the row's data out) -----------------------------


def earnings_summary(
    symbol: str,
    calendar: list[dict[str, Any]],
    eps_history: list[dict[str, Any]],
    *,
    today: date,
) -> dict[str, Any]:
    """The next report in the calendar window and the last reported surprise."""
    upcoming = sorted(
        (
            (day, r)
            for r in _for(calendar, symbol)
            if (day := _day(r.get("report_date"))) is not None and day >= today
        ),
        key=lambda pair: pair[0],
    )
    out: dict[str, Any] = {
        "next_report_date": None,
        "window_end": _iso(today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)),
    }
    if upcoming:
        day, row = upcoming[0]
        code = (row.get("reporting_time") or "").strip().lower()
        out |= {
            "next_report_date": day.isoformat(),
            "report_time": REPORT_TIMES.get(code, "time not supplied"),
            "eps_consensus": _num(row.get("eps_consensus")),
            "num_estimates": _num(row.get("num_estimates")),
            "period_ending": row.get("period_ending"),
        }
    reported = sorted(
        (
            (day, r)
            for r in _for(eps_history, symbol)
            if (day := _day(r.get("date"))) is not None
            and day <= today
            and _num(r.get("eps_actual")) is not None
        ),
        key=lambda pair: pair[0],
    )
    if reported:
        day, row = reported[-1]
        actual, estimate = _num(row.get("eps_actual")), _num(row.get("eps_estimated"))
        surprise = _num(row.get("surprise_percent"))
        if surprise is None and actual is not None and estimate:
            surprise = (actual - estimate) / abs(estimate)
        out["last_report"] = {
            "date": day.isoformat(),
            "eps_actual": actual,
            "eps_estimated": estimate,
            "surprise_frac": surprise,
        }
    return out


def _mid(row: dict[str, Any]) -> float | None:
    mark = _num(row.get("mark"))
    if mark and mark > 0:
        return mark
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2
    last = _num(row.get("last_trade_price"))
    return last if last and last > 0 else None


def options_summary(chain: list[dict[str, Any]], *, today: date) -> dict[str, Any] | None:
    """ATM IV, implied move and skew at the nearest expiry a day or more out."""
    spot = next(
        (s for r in chain if (s := _num(r.get("underlying_price"))) and s > 0),
        None,
    )
    if spot is None:
        return None
    by_expiry: dict[date, list[dict[str, Any]]] = {}
    for row in chain:
        expiry = _day(row.get("expiration"))
        strike = _num(row.get("strike"))
        kind = str(row.get("option_type") or "").lower()
        if expiry is None or strike is None or kind not in ("call", "put") or expiry <= today:
            continue
        by_expiry.setdefault(expiry, []).append({**row, "strike": strike, "option_type": kind})
    for expiry in sorted(by_expiry):
        rows = by_expiry[expiry]
        calls = {r["strike"]: r for r in rows if r["option_type"] == "call"}
        puts = {r["strike"]: r for r in rows if r["option_type"] == "put"}
        paired = [k for k in calls if k in puts and _mid(calls[k]) and _mid(puts[k])]
        if not paired:
            continue
        atm = min(paired, key=lambda k: (abs(k - spot), k))
        straddle = (_mid(calls[atm]) or 0.0) + (_mid(puts[atm]) or 0.0)
        ivs = [
            iv
            for leg in (calls[atm], puts[atm])
            if (iv := _num(leg.get("implied_volatility"))) is not None and iv > 0
        ]
        skew = None
        put_ivs = [(k, _num(r.get("implied_volatility"))) for k, r in puts.items()]
        call_ivs = [(k, _num(r.get("implied_volatility"))) for k, r in calls.items()]
        put_ivs = [(k, iv) for k, iv in put_ivs if iv]
        call_ivs = [(k, iv) for k, iv in call_ivs if iv]
        if put_ivs and call_ivs:
            put_iv = min(put_ivs, key=lambda p: abs(p[0] - spot * SKEW_PUT))[1]
            call_iv = min(call_ivs, key=lambda c: abs(c[0] - spot * SKEW_CALL))[1]
            skew = put_iv - call_iv
        return {
            "expiration": expiry.isoformat(),
            "dte": (expiry - today).days,
            "spot": spot,
            "atm_strike": atm,
            "atm_iv": sum(ivs) / len(ivs) if ivs else None,
            "implied_move_frac": straddle / spot,
            "put_call_skew": skew,
        }
    return None


def fundamentals_summary(
    symbol: str,
    *,
    price_target: list[dict[str, Any]],
    ratios: list[dict[str, Any]],
    insider: list[dict[str, Any]],
    short_interest: list[dict[str, Any]],
    today: date,
) -> dict[str, Any] | None:
    """A compact dated note; None when every part came back empty."""
    out: dict[str, Any] = {}
    target = next(iter(_for(price_target, symbol)), None)
    if target is not None and _num(target.get("target_consensus")) is not None:
        out["price_target"] = {
            "consensus": _num(target.get("target_consensus")),
            "median": _num(target.get("target_median")),
            "low": _num(target.get("target_low")),
            "high": _num(target.get("target_high")),
            "rating": target.get("consensus_rating"),
        }
    dated = sorted(
        (r for r in ratios if _day(r.get("period_ending") or r.get("date")) is not None),
        key=lambda r: _day(r.get("period_ending") or r.get("date")) or date.min,
    )
    if dated:
        latest = dated[-1]
        out["ratios"] = {
            "period_ending": _iso(_day(latest.get("period_ending") or latest.get("date"))),
            "net_profit_margin": _num(latest.get("net_profit_margin")),
            "return_on_equity": _num(latest.get("return_on_equity")),
        }
    since = today - timedelta(days=INSIDER_WINDOW_DAYS)
    buys = sells = 0
    net = 0.0
    for row in _for(insider, symbol):
        day = _day(row.get("transaction_date") or row.get("filing_date"))
        if day is None or not since <= day <= today:
            continue
        shares = _num(row.get("securities_transacted")) or 0.0
        code = str(row.get("acquisition_or_disposition") or "").upper()[:1]
        if code == "A":
            buys += 1
            net += shares
        elif code == "D":
            sells += 1
            net -= shares
    if buys or sells:
        out["insider"] = {
            "window_days": INSIDER_WINDOW_DAYS,
            "buys": buys,
            "sells": sells,
            "net_shares": net,
        }
    settled = sorted(
        (r for r in _for(short_interest, symbol) if _day(r.get("settlement_date")) is not None),
        key=lambda r: _day(r.get("settlement_date")) or date.min,
    )
    if settled:
        latest = settled[-1]
        change = _num(latest.get("change_pct"))
        out["short_interest"] = {
            "settlement_date": _iso(_day(latest.get("settlement_date"))),
            "shares": _num(latest.get("current_short_position")),
            "days_to_cover": _num(latest.get("days_to_cover")),
            "change_frac": change / 100 if change is not None else None,
        }
    return out or None


# --- breakers ----------------------------------------------------------------------------


@dataclass
class ProviderBreaker:
    """Consecutive failures of one provider; open (skipped) for a cooldown once tripped."""

    failures: int = 0
    open_until: datetime | None = None

    def allows(self, now: datetime) -> bool:
        return self.open_until is None or now >= self.open_until

    def success(self) -> None:
        self.failures, self.open_until = 0, None

    def failure(self, now: datetime) -> None:
        self.failures += 1
        if self.failures >= BREAKER_THRESHOLD:
            self.open_until = now + BREAKER_COOLDOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "open_until": self.open_until.isoformat() if self.open_until else None,
        }


class _Run:
    """One refresh's bookkeeping."""

    def __init__(self) -> None:
        self.written: dict[str, list[str]] = {EARNINGS: [], OPTIONS: [], FUNDAMENTALS: []}
        self.failed: list[dict[str, Any]] = []
        self.skipped: set[str] = set()


class OpenBBProducer:
    """Fetch, summarise, append. The single writer of ``openbb_snapshots``."""

    def __init__(
        self,
        db: Database,
        client: OpenBBClient | None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._repo = OpenBBRepo(db)
        self._client = client  # None without TRADER_OPENBB_BASE_URL
        self._clock = clock
        # In-process: a restart forgets them, which costs at most a few retries.
        self.breakers: dict[str, ProviderBreaker] = {}

    async def refresh(self, symbols: Iterable[str]) -> dict[str, Any]:
        tickers = sorted({s.strip().upper() for s in symbols if s.strip()})
        if self._client is None:
            return {"status": "unavailable", "reason": "TRADER_OPENBB_BASE_URL is not set"}
        if not tickers:
            return {"status": "ok", "reason": "no US equities to refresh", "written": {}}
        now = self._clock()
        today = now.date()
        run = _Run()
        try:
            calendar = await self._call(
                run,
                "nasdaq",
                "calendar",
                now,
                start_date=today.isoformat(),
                end_date=(today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)).isoformat(),
            )
            # Symbols in parallel (the timer's CLI call waits on the whole refresh);
            # each symbol's routes in turn.
            await asyncio.gather(
                *(self._symbol(ticker, calendar, today, now, run) for ticker in tickers)
            )
        except SidecarUnreachable as exc:
            log.warning("openbb_sidecar_unreachable", error=str(exc))
            return {**self._report(run), "status": "unavailable", "reason": str(exc)}
        return {**self._report(run), "status": "ok"}

    def _report(self, run: _Run) -> dict[str, Any]:
        return {
            "written": {kind: sorted(done) for kind, done in run.written.items()},
            "failed": run.failed,
            "skipped_providers": sorted(run.skipped),
            "breakers": {name: b.as_dict() for name, b in sorted(self.breakers.items())},
        }

    async def _call(
        self, run: _Run, provider: str, route: str, now: datetime, **params: Any
    ) -> list[dict[str, Any]] | None:
        """One route on one provider through its breaker; None = no answer."""
        assert self._client is not None
        breaker = self.breakers.setdefault(provider, ProviderBreaker())
        if not breaker.allows(now):
            run.skipped.add(provider)
            return None
        try:
            rows = await self._client.fetch(ROUTES[route], provider=provider, **params)
        except SidecarUnreachable:
            raise
        except OpenBBError as exc:
            breaker.failure(now)
            run.failed.append(
                {
                    "provider": provider,
                    "route": route,
                    "symbol": params.get("symbol"),
                    "error": str(exc),
                }
            )
            log.warning("openbb_fetch_failed", provider=provider, route=route, error=str(exc))
            return None
        breaker.success()
        return rows

    async def _symbol(
        self,
        ticker: str,
        calendar: list[dict[str, Any]] | None,
        today: date,
        now: datetime,
        run: _Run,
    ) -> None:
        # No calendar, no earnings row: the last one ages, and the blackout says so.
        if calendar is not None:
            await self._earnings(ticker, calendar, today, now, run)
        await self._options(ticker, today, now, run)
        await self._fundamentals(ticker, today, now, run)

    async def _earnings(
        self, ticker: str, calendar: list[dict[str, Any]], today: date, now: datetime, run: _Run
    ) -> None:
        history = await self._call(run, "nasdaq", "eps", now, symbol=ticker) or []
        data = earnings_summary(ticker, calendar, history, today=today)
        await self._repo.insert(kind=EARNINGS, symbol=ticker, provider="nasdaq", data=data)
        run.written[EARNINGS].append(ticker)

    async def _options(self, ticker: str, today: date, now: datetime, run: _Run) -> None:
        for provider in ("cboe", "nasdaq"):
            chain = await self._call(run, provider, "chains", now, symbol=ticker)
            if chain is None:
                continue
            data = options_summary(chain, today=today)
            if data is not None:
                await self._repo.insert(kind=OPTIONS, symbol=ticker, provider=provider, data=data)
                run.written[OPTIONS].append(ticker)
            return

    async def _fundamentals(self, ticker: str, today: date, now: datetime, run: _Run) -> None:
        parts = {
            "price_target": ("nasdaq", "price_target"),
            "ratios": ("nasdaq", "ratios"),
            "insider": ("sec", "insider"),
            "short_interest": ("finra", "short_interest"),
        }
        got: dict[str, list[dict[str, Any]]] = {}
        answered: list[str] = []
        for name, (provider, route) in parts.items():
            rows = await self._call(run, provider, route, now, symbol=ticker)
            got[name] = rows or []
            if rows is not None and provider not in answered:
                answered.append(provider)
        data = fundamentals_summary(ticker, today=today, **got)
        if data is not None:
            await self._repo.insert(
                kind=FUNDAMENTALS, symbol=ticker, provider=",".join(answered), data=data
            )
            run.written[FUNDAMENTALS].append(ticker)
