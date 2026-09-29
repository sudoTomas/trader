"""Replay eval harness — rerun the decide stage over history, offline.

The gate-keeper for every future decide-behavior change (prompt edits, model swaps, the
desk): replay a window under the candidate configuration, score it against a baseline,
and only then open a live shadow window (docs/runbooks/decide-change-gate.md;
see docs/lessons.md#replay-before-flip).

Structure:
- ``ReplayMarketData`` — the MarketData protocol served from the bars cache, frozen at an
  ``as_of`` instant. The future can never leak into a replayed context, and prompts are
  deterministic for the same inputs (asserted by test).
- ``ReplayHarness`` — walks a window step by step, builds each context with the SAME
  ContextBuilder/Decider the live loop uses, and journals to ``eval_runs``/``eval_steps``
  — never ``decisions``, never anywhere near an order path.

Scoring is deliberately crude and labeled as such: mark-to-bars, no costs, no slippage
(Alpaca paper fills are optimistic already — v1 findings). It ranks configurations; it
does not predict live PnL.

Research lanes are part of the replayed prompt, pinned to the step: the Macro View
whose ``created_at`` is at or before that timestamp (never today's ``current()``),
and the symbol's brief whose ``ts`` is likewise. ``config={"briefs": false}`` drops
the brief lane so a baseline can still match the dark live path, and
``config={"macro_view": false}`` drops the Macro View lane the same way, so either
lane can be scored alone. Replay writes ``eval_runs`` / ``eval_steps`` only — it
does not flip briefs into the live loop.
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
from trader.store.repos import BriefsRepo, EvalRepo, MacroViewsRepo

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
        desk: Any | None = None,  # desk.desk.Desk; enables config={"desk": true} runs
    ) -> None:
        self._db = db
        self._repo = EvalRepo(db)
        self._decider = Decider(llm)
        self._market = ReplayMarketData(BarsCache(db, provider))
        # Own builders: the live runner keeps briefs dark unless TRADER_BRIEFS_ENABLED.
        # Attaching the repos here only affects replayed prompts. The builder has a
        # per-call brief switch but no macro one, so a macro-less run gets a builder
        # with no Macro View repo at all — the lane is absent, not blanked.
        self._contexts = {
            include_macro_view: ContextBuilder(
                self._market,
                macro_views=MacroViewsRepo(db) if include_macro_view else None,
                briefs=BriefsRepo(db),
                # ReplayMarketData is the bars view. Macro series stay on the real
                # provider and are capped at the step's date inside ContextBuilder.
                fred=getattr(provider, "fred", None),
            )
            for include_macro_view in (True, False)
        }
        self._step = timedelta(seconds=step_seconds)
        self._max_steps = max_steps
        self._equity = equity
        self._desk = desk

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
        if (config or {}).get("desk") and self._desk is None:
            raise ValueError("desk replay requested but no desk is wired into this harness")
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
        config = run["config"] or {}
        use_desk = bool(config.get("desk"))
        # Research lanes default on so the gate sees them. Opt out of briefs to match
        # live, where that lane stays dark until its own flip; opt out of the Macro
        # View to score the brief lane on its own.
        include_briefs = bool(config.get("briefs", True))
        include_macro_view = bool(config.get("macro_view", True))
        try:
            summary = await self._walk(
                run_id,
                symbol,
                run["start_ts"],
                run["end_ts"],
                use_desk=use_desk,
                include_briefs=include_briefs,
                include_macro_view=include_macro_view,
            )
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
        self,
        run_id: int,
        symbol: Symbol,
        start: datetime,
        end: datetime,
        *,
        use_desk: bool,
        include_briefs: bool,
        include_macro_view: bool = True,
    ) -> dict[str, Any]:
        context = self._contexts[include_macro_view]
        actions: dict[str, int] = {}
        desk_tally = {"approve": 0, "shrink": 0, "veto": 0, "pass_through": 0, "skipped": 0}
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
            ctx = await context.build(
                symbol,
                now=as_of,
                triggers=[Trigger(REPLAY_TRIGGER, symbol.ticker)],
                position_qty=qty,
                equity=self._equity + realized,
                recent_decisions=list(recent),
                research_as_of=as_of,
                include_briefs=include_briefs,
            )
            if ctx.price <= 0:
                skipped_no_data += 1
                as_of += self._step
                continue
            last_price = ctx.price

            desk_info: dict[str, Any] | None = None
            if use_desk:
                # Unlike the live loop's fail-open wrapping, a replay desk failure
                # propagates: a desk-on run that silently degrades to baseline would
                # corrupt the comparison the run exists to make.
                ctx, desk_info = await self._desk.enrich(ctx)

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
            if use_desk:
                decision, desk_info = await self._desk.review(ctx, decision, desk_info or {})
                desk_tally[desk_info["critic"]["verdict"]] += 1
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

            # Versions ride the step decision: the eval listing omits the prompt, and
            # the gate has to see which research row the step actually used.
            decision_row: dict[str, Any] = {
                "action": decision.action.value,
                "conviction": decision.conviction,
                "rationale": decision.rationale,
            }
            if ctx.macro_view is not None:
                decision_row["macro_view_version"] = ctx.macro_view.version
                decision_row["macro_view"] = ctx.macro_view.entries
            if ctx.brief is not None:
                decision_row["brief_version"] = ctx.brief.version
                decision_row["brief"] = ctx.brief.entries
            if desk_info is not None:
                decision_row["desk"] = desk_info
            await self._repo.add_step(
                run_id,
                ts=as_of,
                status="decided",
                price=ctx.price,
                prompt=result.prompt,
                raw_output=result.raw_output,
                decision=decision_row,
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
            **({"desk": desk_tally} if use_desk else {}),
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
