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

Scoring is deliberately crude and labeled as such: mark-to-bars, one position (Alpaca
paper fills are optimistic already — v1 findings). It ranks configurations; it does not
predict live PnL. Every fill pays the cost model the harness is given
(``trader.eval.costs``, #139: half-spread, fees, square-root impact), and the summary
reports ``gross_pnl`` and ``net_pnl`` side by side; ``realized_pnl`` / ``unrealized_pnl``
stay gross. A harness built without a cost model fills for free.

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

A Bot version's decide rules and Sleeve replay the same way (#127):
``config={"prompt_variant": "equity-largecap", "sleeve_usd": 5000}`` judges every step by
that variant's system prompt and sizes it against a fixed Sleeve (the prompt then reads
``Sleeve equity``, as the live loop renders it). Absent, a run is Bot #1's: the
``default`` rules against the harness equity.

``config={"blind": true}`` is a blinded run (#140, ``trader.eval.blind``): every prompt
withholds the ticker (a per-run alias), calendar dates (relative bar labels) and dollar
values (a price index, sizes as % of the Sleeve). The model's %-of-Sleeve size is mapped
back to dollars after the desk and before the crude book, so the stop, the min hold, the
no-trade band and the costs run unchanged. The Macro View and briefs are dropped (free
text that names the asset); the summary's ``blind`` records which Sources were. Every
step records the ``intent`` the book acted on, so a named and a blind run compare
directly. Live prompts never blind.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import structlog

from trader.core.symbols import Symbol
from trader.core.types import Bar, Quote
from trader.data.bars_cache import BarsCache
from trader.data.base import MarketData, MarketDataError
from trader.eval.blind import DEFAULT_ALIAS, Blinder, names_symbol, split_sources
from trader.eval.costs import FillCost, ReplayCosts
from trader.loop.context import ContextBuilder, DecisionContext
from trader.loop.decider import (
    DEFAULT_VARIANT,
    EXIT_REVIEW_PROMPT,
    PROMPT_VARIANTS,
    Decider,
    DeciderError,
)
from trader.loop.scheduler import Trigger
from trader.loop.sources import (
    bot_one_subscriptions,
    build_sources,
    subscriptions,
    unrealized_pnl_frac,
)
from trader.risk.earnings import applies, earnings_blackout, load_earnings
from trader.risk.limits import RiskLimits, bars_per_day, stop_loss_frac_for
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
        # The live limits, for the deterministic stop (#74): with them a replayed position
        # past its stop is closed at that step before the model is asked, and the stop's
        # cooldown keeps later opens on the symbol out of the crude book. The min hold
        # keeps the model's closes of a fresh position out of it too, the earnings
        # blackout its opens around a report, and the no-trade band its small opens.
        # None = none of them.
        limits: RiskLimits | None = None,
        # What every replayed fill pays (#139). None = free fills.
        costs: ReplayCosts | None = None,
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
        self._limits = limits
        self._costs = costs or ReplayCosts()

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
        if config and config.get("prompt_variant", DEFAULT_VARIANT) not in PROMPT_VARIANTS:
            raise ValueError(
                f"unknown prompt variant {config['prompt_variant']!r}"
                f" (known: {sorted(PROMPT_VARIANTS)})"
            )
        if config and config.get("sleeve_usd") is not None:
            try:
                sleeve = float(config["sleeve_usd"])
            except (TypeError, ValueError) as exc:
                raise ValueError("sleeve_usd must be a positive number") from exc
            if not math.isfinite(sleeve) or sleeve <= 0.0:
                raise ValueError("sleeve_usd must be a positive number")
            config = {**config, "sleeve_usd": sleeve}
        if config and config.get("blind"):
            # The decide rules are a prompt too: rules that name the symbol would
            # un-blind every step (the desk's and the Confirm's rules are generic).
            rules = PROMPT_VARIANTS[config.get("prompt_variant", DEFAULT_VARIANT)]
            if names_symbol(rules + EXIT_REVIEW_PROMPT, symbol):
                raise ValueError(
                    f"prompt variant {config.get('prompt_variant', DEFAULT_VARIANT)!r}"
                    f" names the symbol {symbol.ticker}; a blind run cannot use it"
                )
            config = {**config, "blind": True}
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
        prompt_variant = config.get("prompt_variant", DEFAULT_VARIANT)
        sleeve = config.get("sleeve_usd")
        blind: dict[str, Any] | None = None
        if config.get("blind"):
            sources, dropped = split_sources(sources)
            blind = {
                "alias": DEFAULT_ALIAS,
                "sources_dropped": dropped,
                "sources_kept": sorted({s["source"] for s in sources}),
            }
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
                prompt_variant=prompt_variant,
                sleeve=float(sleeve) if sleeve is not None else None,
                blind=blind,
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

    def _stop(
        self, symbol: Symbol, price: float, vol: float | None, qty: float, entry: float
    ) -> str | None:
        """The stop's rationale when the sim's position is past its stop, else None."""
        if self._limits is None or qty == 0.0:
            return None
        loss = unrealized_pnl_frac(price, qty, entry)
        frac = stop_loss_frac_for(self._limits, vol, symbol.asset_class)
        if loss is None or frac <= 0 or loss > -frac:
            return None
        return f"stop-loss: {loss:+.1%} against entry is past -{frac:.1%}"

    async def _earnings_blackout(self, symbol: Symbol, as_of: datetime) -> bool:
        """RiskGuard's earnings blackout at ``as_of``, from the calendar row fetched by
        then (no row then = fails open, as live)."""
        days = self._limits.earnings_blackout_days if self._limits is not None else 0
        if days <= 0 or not applies(symbol):
            return False
        earnings = await load_earnings(self._db, symbol.ticker, as_of)
        return earnings_blackout(earnings, symbol, as_of, days).blocked

    async def _fill_cost(self, symbol: Symbol, ctx: DecisionContext, notional: float) -> FillCost:
        """What a fill of ``notional`` at this step pays. Liquidity comes from the step's
        own 1h bars: average daily dollar volume is the mean bar volume times the bars in
        the symbol's day at the step's price; the daily vol is the bar vol scaled the same
        way (as the stop scales it)."""
        bid = ask = None
        try:
            quote = await self._market.get_latest_quote(symbol)
            bid, ask = quote.bid, quote.ask
        except MarketDataError:
            pass
        day = bars_per_day(symbol.asset_class)
        bars = next(iter(ctx.bars.values()), [])
        adv = (sum(b.volume for b in bars) / len(bars)) * day * ctx.price if bars else None
        vol = ctx.primary_vol
        return self._costs.fill(
            symbol.asset_class,
            notional,
            bid=bid,
            ask=ask,
            adv_usd=adv,
            daily_vol=vol * math.sqrt(day) if vol is not None else None,
        )

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
        prompt_variant: str = DEFAULT_VARIANT,
        sleeve: float | None = None,
        blind: dict[str, Any] | None = None,  # the run's blind record; None = named
    ) -> dict[str, Any]:
        context = self._context
        blinder = Blinder(symbol, step=self._step) if blind is not None else None
        equity = self._equity if sleeve is None else sleeve
        actions: dict[str, int] = {}
        desk_tally = {"approve": 0, "shrink": 0, "veto": 0, "pass_through": 0, "skipped": 0}
        recent: list[dict[str, Any]] = []
        steps = errors = skipped_closed = skipped_no_data = 0
        stops = stop_cooldown_denied = min_hold_denied = no_trade_band_denied = 0
        earnings_blackout_denied = 0
        paid = FillCost()  # what the fills cost, summed
        stopped_at: datetime | None = None
        qty = entry = realized = cost = 0.0
        last_price = 0.0
        # The sim's one-position book. A seed pre-loads it so the exit-review block has a
        # position to review from the first step; opened_at dates the position for the
        # block's age line (the seed's own, else the window start; the sim's own opens
        # stamp the step they land on).
        opened_at: datetime | None = None
        # When the min hold runs from (RiskGuard's min_hold): the sim's own open, or a seed
        # that says when it was opened. A seed of unknown age is not held.
        held_from: datetime | None = None
        if seed is not None:
            qty = seed["qty"]
            entry = seed["avg_entry_price"]
            opened_at = (
                datetime.fromisoformat(seed["opened_at"]) if seed.get("opened_at") else start
            )
            if seed.get("opened_at"):
                held_from = opened_at

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
                equity=equity + realized,
                recent_decisions=list(recent),
                as_of=as_of,
                subscriptions=sources,
                position=(
                    {"qty": qty, "avg_entry_price": entry, "opened_at": opened_at}
                    if qty != 0.0
                    else None
                ),
            )
            # The live loop's Bot overrides (runner._wakeup), applied the same way.
            if prompt_variant != DEFAULT_VARIANT:
                ctx = replace(ctx, prompt_variant=prompt_variant)
            if sleeve is not None:
                ctx = replace(ctx, equity_label="Sleeve equity")
            if blinder is not None and ctx.price > 0:
                ctx = blinder.blind(
                    ctx,
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

            stop = self._stop(symbol, ctx.price, ctx.primary_vol, qty, entry)
            if stop is not None:
                # The stop pre-empts the model, as live. One step's mark (a bar close) stands
                # in for the live loop's two 60 s ticks.
                fill = await self._fill_cost(symbol, ctx, abs(qty) * ctx.price)
                paid += fill
                realized += (ctx.price - entry) * qty
                qty = 0.0
                opened_at = held_from = None
                stopped_at = as_of
                stops += 1
                actions["close"] = actions.get("close", 0) + 1
                await self._repo.add_step(
                    run_id,
                    ts=as_of,
                    status="decided",
                    price=ctx.price,
                    decision={
                        "action": "close",
                        "conviction": 1.0,
                        "rationale": stop,
                        "trigger": "stop_loss",
                        "fill_cost": fill.as_dict(),
                    },
                    latency_ms=0,
                )
                steps += 1
                recent.append(
                    {
                        "ts": as_of.isoformat(),
                        "trigger": "stop_loss",
                        "status": "replay",
                        "decision": {"action": "close"},
                    }
                )
                recent[:] = recent[-5:]
                as_of += self._step
                continue

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
            if blinder is not None:
                # The model sized in % of the Sleeve (the critic shrank in the same unit):
                # dollars from here on, so the book and its RiskGuard rules run unchanged.
                decision = blinder.unblind(decision, equity=ctx.equity)
            actions[decision.action.value] = actions.get(decision.action.value, 0) + 1
            cost += result.completion.cost_usd or 0.0

            # Crude sim: one position, mark-to-bars, every fill pays the cost model.
            # Ranks configs only.
            cooling = (
                stopped_at is not None
                and self._limits is not None
                and (as_of - stopped_at).total_seconds() < self._limits.stop_cooldown_seconds
            )
            holding = (
                held_from is not None
                and self._limits is not None
                and (as_of - held_from).total_seconds() < self._limits.min_hold_seconds
            )
            # RiskGuard's no-trade band, against the equity the prompt was sized from.
            band = ctx.equity * self._limits.no_trade_band_frac if self._limits else 0.0
            held = banded = blacked = False
            fill: FillCost | None = None
            if decision.intent is not None:
                side = 1.0 if decision.intent.side.value == "buy" else -1.0
                if decision.action.value == "open" and qty == 0.0 and cooling:
                    stop_cooldown_denied += 1  # RiskGuard's stop_cooldown would deny it
                elif (
                    decision.action.value == "open"
                    and qty == 0.0
                    and await self._earnings_blackout(symbol, as_of)
                ):
                    blacked = True
                    earnings_blackout_denied += 1  # RiskGuard's earnings_blackout would deny it
                elif (
                    decision.action.value == "open"
                    and qty == 0.0
                    and (decision.intent.notional < band)
                ):
                    banded = True
                    no_trade_band_denied += 1  # RiskGuard's no_trade_band would deny it
                elif decision.action.value == "open" and qty == 0.0:
                    fill = await self._fill_cost(symbol, ctx, decision.intent.notional)
                    qty = side * decision.intent.notional / ctx.price
                    entry = ctx.price
                    opened_at = held_from = as_of
                elif decision.action.value == "close" and qty != 0.0 and holding:
                    held = True
                    min_hold_denied += 1  # RiskGuard's min_hold would deny it
                elif decision.action.value == "close" and qty != 0.0:
                    fill = await self._fill_cost(symbol, ctx, abs(qty) * ctx.price)
                    realized += (ctx.price - entry) * qty
                    qty = 0.0
                    opened_at = held_from = None
            if fill is not None:
                paid += fill

            # Versions ride the step decision: the eval listing omits the prompt, and
            # the gate has to see which research row the step actually used.
            decision_row: dict[str, Any] = {
                "action": decision.action.value,
                "conviction": decision.conviction,
                "rationale": decision.rationale,
            }
            if decision.intent is not None:
                # What RiskGuard would see (blind: already mapped back to dollars).
                decision_row["intent"] = {
                    "symbol": decision.intent.symbol,
                    "side": decision.intent.side.value,
                    "notional": decision.intent.notional,
                }
            if held:
                decision_row["min_hold"] = True
            if banded:
                decision_row["no_trade_band"] = True
            if blacked:
                decision_row["earnings_blackout"] = True
            if fill is not None:
                decision_row["fill_cost"] = fill.as_dict()
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

        unrealized = round((last_price - entry) * qty, 6) if qty else 0
        gross = realized + unrealized
        return {
            **({"desk": desk_tally} if use_desk else {}),
            **({"exit_review_seed": seed} if seed is not None else {}),
            "exit_review": exit_review,
            "prompt_variant": prompt_variant,
            **({"sleeve_usd": sleeve} if sleeve is not None else {}),
            **({"blind": blind} if blind is not None else {}),
            "steps": steps,
            "stops": stops,
            "stop_cooldown_denied": stop_cooldown_denied,
            "min_hold_denied": min_hold_denied,
            "no_trade_band_denied": no_trade_band_denied,
            "earnings_blackout_denied": earnings_blackout_denied,
            "errors": errors,
            "skipped_closed": skipped_closed,
            "skipped_no_data": skipped_no_data,
            "actions": actions,
            "realized_pnl": round(realized, 6),
            "unrealized_pnl": unrealized,
            # Gross and net side by side (#139): net is gross less what the fills paid. A
            # position still open has paid its entry, not its exit.
            "gross_pnl": round(gross, 6),
            "trading_costs": paid.as_dict(),
            "net_pnl": round(gross - paid.total, 6),
            "cost_model": self._costs.as_dict(),
            "cost_usd": round(cost, 6),  # the model calls, not the fills
            "pnl_model": (
                "mark-to-bars, one position, fills pay cost_model — ranks configs, not live PnL"
            ),
        }
