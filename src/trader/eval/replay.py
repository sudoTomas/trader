"""Replay eval harness (EAI-188) — rerun the decide stage over history, offline.

The gate-keeper for every future decide-behavior change (prompt edits, model swaps, the
desk): replay a window under the candidate configuration, score it against a baseline,
and only then open a live shadow window (docs/runbooks/decide-change-gate.md).

Structure:
- ``ReplayMarketData`` — the MarketData protocol served from the bars cache, frozen at an
  ``as_of`` instant. The future can never leak into a replayed context, and prompts are
  deterministic for the same inputs (asserted by test).
- ``ReplayHarness`` — walks a window step by step, builds each context with the SAME
  ContextBuilder/Decider the live loop uses, and journals to ``eval_runs``/``eval_steps``
  — never ``decisions``, never anywhere near an order path.

Scoring is deliberately crude and labeled as such: mark-to-bars, no costs, no slippage
(Alpaca paper fills are optimistic already — v1 findings). It ranks configurations; it
does not predict live PnL. The Macro View lane is currently excluded from replayed
contexts (historical versions are addressable via ``macro_view_version`` — future work).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import structlog

from trader.core.symbols import Symbol
from trader.core.types import Bar, Quote
from trader.data.bars_cache import BarsCache
from trader.data.base import MarketData, MarketDataError
from trader.loop.context import ContextBuilder
from trader.loop.decider import Decider, DeciderError
from trader.loop.scheduler import Trigger
from trader.store.db import Database
from trader.store.repos import EvalRepo

log = structlog.get_logger(__name__)

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
DEFAULT_MAX_STEPS = 500
REPLAY_TRIGGER = "replay"


class ReplayMarketData:
    """MarketData over the bars cache, frozen at ``as_of``. Set ``as_of`` per step."""

    name = "replay"

    def __init__(self, cache: BarsCache) -> None:
        self._cache = cache
        self.as_of: datetime | None = None

    async def get_bars(self, symbol: Symbol, interval: str, lookback: int) -> list[Bar]:
        assert self.as_of is not None, "set as_of before serving a replay step"
        step = timedelta(seconds=INTERVAL_SECONDS[interval])
        # +2 bars of slack so the cache's edge-based coverage test can hold at both ends.
        start = self.as_of - step * (lookback + 2)
        bars = await self._cache.ensure_bars(symbol, interval, start, self.as_of)
        # Strictly before as_of: a bar whose open time has arrived may still be forming
        # at as_of, and a replay must never see data the live loop could not have.
        return [b for b in bars if b.ts < self.as_of][-lookback:]

    async def get_latest_quote(self, symbol: Symbol) -> Quote:
        assert self.as_of is not None
        bars = await self.get_bars(symbol, "1h", 1)
        if not bars:
            raise MarketDataError(f"no cached bars for {symbol.ticker} at {self.as_of}")
        last = bars[-1]
        return Quote(symbol=symbol.ticker, ts=self.as_of, bid=last.close, ask=last.close)


class ReplayHarness:
    """Create/execute replay runs. ``run()`` is the inline convenience; the service
    layer uses ``create_run()`` + background ``execute()`` so surfaces return fast."""

    def __init__(
        self,
        db: Database,
        llm: Any,  # LLMRegistry-shaped; Nemotron's decide role by default (free)
        provider: MarketData,
        *,
        step_seconds: float = 3600.0,
        max_steps: int = DEFAULT_MAX_STEPS,
        equity: float = 100_000.0,
    ) -> None:
        self._db = db
        self._repo = EvalRepo(db)
        self._decider = Decider(llm)
        self._market = ReplayMarketData(BarsCache(db, provider))
        self._context = ContextBuilder(self._market)
        self._step = timedelta(seconds=step_seconds)
        self._max_steps = max_steps
        self._equity = equity

    # -- public API ----------------------------------------------------------

    async def create_run(
        self,
        symbol: Symbol,
        start: datetime,
        end: datetime,
        *,
        label: str,
        config: dict[str, Any] | None = None,
    ) -> int:
        n_steps = int((end - start) / self._step)
        if n_steps > self._max_steps:
            raise ValueError(
                f"window is {n_steps} steps; max is {self._max_steps} — narrow it or raise"
                " max_steps deliberately (LLM cost scales linearly)"
            )
        return await self._repo.create_run(
            label=label,
            symbol=symbol.ticker,
            interval="1h",
            start_ts=start,
            end_ts=end,
            config=config or {},
        )

    async def execute(self, run_id: int, symbol: Symbol) -> dict[str, Any]:
        """Walk the run's window. Always finishes the row (done/failed)."""
        run = await self._repo.get_run(run_id)
        assert run is not None
        try:
            summary = await self._walk(run_id, symbol, run["start_ts"], run["end_ts"])
            await self._repo.finish_run(run_id, status="done", summary=summary)
        except Exception as exc:  # noqa: BLE001 - a failed run must say so, not hang
            log.exception("replay_run_failed", run_id=run_id)
            await self._repo.finish_run(run_id, status="failed", summary={"error": str(exc)})
        row = await self._repo.get_run(run_id)
        assert row is not None
        return row

    async def run(
        self,
        symbol: Symbol,
        start: datetime,
        end: datetime,
        *,
        label: str,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = await self.create_run(symbol, start, end, label=label, config=config)
        return await self.execute(run_id, symbol)

    # -- internals -----------------------------------------------------------

    async def _walk(
        self, run_id: int, symbol: Symbol, start: datetime, end: datetime
    ) -> dict[str, Any]:
        actions: dict[str, int] = {}
        recent: list[dict[str, Any]] = []
        steps = errors = skipped_closed = skipped_no_data = 0
        qty = entry = realized = cost = 0.0
        last_price = 0.0

        as_of = start
        while as_of < end:
            if not symbol.market_calendar.is_open(as_of):
                skipped_closed += 1
                as_of += self._step
                continue

            self._market.as_of = as_of
            ctx = await self._context.build(
                symbol,
                now=as_of,
                triggers=[Trigger(REPLAY_TRIGGER, symbol.ticker)],
                position_qty=qty,
                equity=self._equity + realized,
                recent_decisions=list(recent),
            )
            if ctx.price <= 0:
                skipped_no_data += 1
                as_of += self._step
                continue
            last_price = ctx.price

            started = time.perf_counter()
            try:
                result = await self._decider.decide(ctx)
            except DeciderError as exc:
                errors += 1
                await self._repo.add_step(
                    run_id,
                    ts=as_of,
                    status="error",
                    price=ctx.price,
                    prompt=ctx.to_prompt(),
                    raw_output=str(exc),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
                as_of += self._step
                continue

            decision = result.decision
            actions[decision.action.value] = actions.get(decision.action.value, 0) + 1
            cost += result.completion.cost_usd or 0.0

            # Crude sim: one position, mark-to-bars, no costs. Ranks configs only.
            if decision.intent is not None:
                side = 1.0 if decision.intent.side.value == "buy" else -1.0
                if decision.action.value == "open" and qty == 0.0:
                    qty = side * decision.intent.notional / ctx.price
                    entry = ctx.price
                elif decision.action.value == "close" and qty != 0.0:
                    realized += (ctx.price - entry) * qty
                    qty = 0.0

            await self._repo.add_step(
                run_id,
                ts=as_of,
                status="decided",
                price=ctx.price,
                prompt=result.prompt,
                raw_output=result.raw_output,
                decision={
                    "action": decision.action.value,
                    "conviction": decision.conviction,
                    "rationale": decision.rationale,
                },
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            steps += 1
            recent.append(
                {
                    "ts": as_of.isoformat(),
                    "trigger": REPLAY_TRIGGER,
                    "status": "replay",
                    "decision": {"action": decision.action.value},
                }
            )
            recent[:] = recent[-5:]
            as_of += self._step

        return {
            "steps": steps,
            "errors": errors,
            "skipped_closed": skipped_closed,
            "skipped_no_data": skipped_no_data,
            "actions": actions,
            "realized_pnl": round(realized, 6),
            "unrealized_pnl": round((last_price - entry) * qty, 6) if qty else 0,
            "cost_usd": round(cost, 6),
            "pnl_model": "mark-to-bars, no costs/slippage — ranks configs, not live PnL",
        }
