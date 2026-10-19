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

Replay reads the same Sources the live loop does (#124), every one pinned to the step
(``as_of``): the Macro View whose ``created_at`` is at or before that timestamp (never
today's ``current()``), the symbol's brief whose ``ts`` is likewise, and so on; each
step's decision records its ``source_versions``. ``config={"sources": [...]}`` replays
exactly a Bot version's subscriptions. Without it the run reads Bot #1's lanes:
``config={"briefs": false}`` drops the brief lane so a baseline can still match the dark
live path, and ``config={"macro_view": false}`` drops the Macro View lane the same way,
so either lane can be scored alone. Replay writes ``eval_runs`` / ``eval_steps`` only —
it does not flip briefs into the live loop.

The exit-review lane (#34) is replayed the same way: ``config={"exit_review": true}``
renders the held-position block (absent → the harness default, which follows
``TRADER_EXIT_REVIEW_ENABLED``, off). Replay starts flat, so a run can **seed** a held
position for the window with ``config={"exit_review_seed": {"qty", "avg_entry_price"}}``
— an in-memory book for the crude sim only, never a ``positions`` row. The crude PnL then
starts from that seed: a close realizes against the seeded entry, and a run that never
closes carries the seed's mark-to-bars move as ``unrealized_pnl``. Seed the SAME
position into the baseline and the candidate; only the block should differ.
"""

from __future__ import annotations

import math
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
from trader.loop.sources import bot_one_subscriptions, build_sources, subscriptions
from trader.store.db import Database
from trader.store.repos import EvalRepo

log = structlog.get_logger(__name__)

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
DEFAULT_MAX_STEPS = 500
REPLAY_TRIGGER = "replay"


def validate_seed(seed: Any) -> dict[str, Any]:
    """The seeded position for an exit-review replay: a non-zero ``qty`` and a positive
    ``avg_entry_price``; ``opened_at`` (ISO-8601, tz-aware) optionally dates it, else
    the position is treated as opened at the window start."""
    if not isinstance(seed, dict):
        raise ValueError("exit_review_seed must be an object with qty and avg_entry_price")
    try:
        qty = float(seed["qty"])
        entry = float(seed["avg_entry_price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("exit_review_seed needs numeric qty and avg_entry_price") from exc
    if not math.isfinite(qty) or qty == 0.0:
        raise ValueError("exit_review_seed qty must be a non-zero finite number")
    if not math.isfinite(entry) or entry <= 0.0:
        raise ValueError("exit_review_seed avg_entry_price must be positive and finite")
    out: dict[str, Any] = {"qty": qty, "avg_entry_price": entry}
    opened_at = seed.get("opened_at")
    if opened_at is not None:
        parsed = datetime.fromisoformat(opened_at) if isinstance(opened_at, str) else opened_at
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            raise ValueError("exit_review_seed opened_at must be a tz-aware ISO-8601 timestamp")
        out["opened_at"] = parsed.isoformat()
    return out


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
        exit_review_enabled: bool = False,  # default for runs that do not say (#34)
    ) -> None:
        self._db = db
        self._repo = EvalRepo(db)
        self._decider = Decider(llm)
        self._market = ReplayMarketData(BarsCache(db, provider))
        # The same Sources the live loop reads, every one wired; each run's subscriptions
        # pick its lanes, and every read is pinned to the step (as_of).
        self._context = ContextBuilder(
            self._market,
            # ReplayMarketData is the bars view. Macro series stay on the real provider
            # and are capped at the step's date by the FRED Source.
            sources=build_sources(db, provider),
        )
        self._step = timedelta(seconds=step_seconds)
        self._max_steps = max_steps
        self._equity = equity
        self._desk = desk
        self._exit_review_default = exit_review_enabled

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
        if config and config.get("sources") is not None:
            try:
                config = {
                    **config,
                    "sources": [s.as_spec() for s in subscriptions(config["sources"])],
                }
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError("sources must be a list of {source, params} objects") from exc
        if config and config.get("exit_review_seed") is not None:
            config = {**config, "exit_review_seed": validate_seed(config["exit_review_seed"])}
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
        # The exit-review lane defaults to the harness setting (live: dark). A seed is
        # independent of the flag so baseline and candidate share the same held book.
        exit_review = bool(config.get("exit_review", self._exit_review_default))
        # A run that names its Sources (a Bot version's list) reads exactly those; the
        # lane switches above only shape the default.
        sources = config.get("sources")
        if sources is None:
            sources = bot_one_subscriptions(
                macro_view=include_macro_view, briefs=include_briefs, exit_review=exit_review
            )
        seed = config.get("exit_review_seed")
        try:
            summary = await self._walk(
                run_id,
                symbol,
                run["start_ts"],
                run["end_ts"],
                use_desk=use_desk,
                sources=sources,
                exit_review=exit_review,
                seed=validate_seed(seed) if seed is not None else None,
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
        sources: list[dict[str, Any]],
        exit_review: bool = False,
        seed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = self._context
        actions: dict[str, int] = {}
        desk_tally = {"approve": 0, "shrink": 0, "veto": 0, "pass_through": 0, "skipped": 0}
        recent: list[dict[str, Any]] = []
        steps = errors = skipped_closed = skipped_no_data = 0
        qty = entry = realized = cost = 0.0
        last_price = 0.0
        # The sim's one-position book. A seed pre-loads it so the exit-review block has a
        # position to review from the first step; opened_at dates the position for the
        # block's age line (the seed's own, else the window start; the sim's own opens
        # stamp the step they land on).
        opened_at: datetime | None = None
        if seed is not None:
            qty = seed["qty"]
            entry = seed["avg_entry_price"]
            opened_at = (
                datetime.fromisoformat(seed["opened_at"]) if seed.get("opened_at") else start
            )

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
                as_of=as_of,
                subscriptions=sources,
                position=(
                    {"qty": qty, "avg_entry_price": entry, "opened_at": opened_at}
                    if qty != 0.0
                    else None
                ),
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
                    opened_at = as_of
                elif decision.action.value == "close" and qty != 0.0:
                    realized += (ctx.price - entry) * qty
                    qty = 0.0
                    opened_at = None

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
            if ctx.held_position is not None:
                decision_row["held_position"] = ctx.held_position.entries
            decision_row["source_versions"] = ctx.source_versions
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
            **({"exit_review_seed": seed} if seed is not None else {}),
            "exit_review": exit_review,
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
