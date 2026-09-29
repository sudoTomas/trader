"""Service layer — the single registry of capabilities.

Every capability the trader exposes is a *query* (read-only) or *command* (state-changing)
registered here exactly once. The REST API, the MCP server, and the CLI are thin adapters
over this registry: one capability, defined once, three doors (docs/architecture.md §3).

Feature milestones add commands/queries here first, then wire the three adapters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from trader.store.db import Database

VERSION = "0.1.0"


@dataclass(frozen=True)
class Capability:
    """One registered service capability."""

    name: str
    kind: str  # "query" | "command"
    handler: Callable[..., Awaitable[Any]]
    summary: str

    @property
    def tool_name(self) -> str:
        """The MCP tool name: queries read as ``get_<name>``, commands keep their name."""
        return f"get_{self.name}" if self.kind == "query" else self.name


@dataclass
class ServiceRegistry:
    """Holds the capability set. One instance is built at startup and shared by all surfaces."""

    _caps: dict[str, Capability] = field(default_factory=dict)

    def register(self, name: str, kind: str, summary: str):
        def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            if name in self._caps:
                raise ValueError(f"capability already registered: {name}")
            self._caps[name] = Capability(name=name, kind=kind, handler=fn, summary=summary)
            return fn

        return deco

    def get(self, name: str) -> Capability:
        return self._caps[name]

    def all(self) -> list[Capability]:
        return list(self._caps.values())


def build_registry(
    db: Database | None = None,
    runner: Any | None = None,
    compiler: Any | None = None,
    mirror: Any | None = None,
    sentiment: Any | None = None,
    evaluator: Any | None = None,
    briefs: Any | None = None,
) -> ServiceRegistry:
    """Construct the registry with the capabilities available in this build.

    ``db`` is the shared (possibly not-yet-opened) pool; handlers only touch it per call,
    so the registry can be built before the lifespan opens the pool. ``runner`` is the
    decide loop, present only when this build runs one; ``compiler`` is the Macro View
    compiler, ``briefs`` the per-candidate brief compiler, and ``mirror`` the one-way
    markdown exporter (research.*), absent in store-less builds.
    """
    reg = ServiceRegistry()

    @reg.register("health", kind="query", summary="Liveness, build, and store reachability.")
    async def health() -> dict[str, Any]:
        if db is None:
            db_status = "unconfigured"
        else:
            try:
                await db.fetch_one("SELECT 1")
                db_status = "ok"
            except Exception:
                db_status = "unreachable"
        status = "ok" if db_status == "ok" else "degraded"
        return {"status": status, "service": "trader", "version": VERSION, "db": db_status}

    @reg.register("audit", kind="query", summary="Recent audit events, newest first.")
    async def audit(limit: int = 50) -> list[dict[str, Any]]:
        assert db is not None, "audit requires a store"
        rows = await db.fetch_all(
            "SELECT id, ts, actor, category, action, subject, payload"
            " FROM audit_events ORDER BY ts DESC, id DESC LIMIT %s",
            (min(limit, 500),),
        )
        # ISO-format timestamps so all three surfaces emit the identical JSON shape.
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]

    @reg.register(
        "cache_stats", kind="query", summary="Bars-cache hit/miss counters (replay path)."
    )
    async def cache_stats() -> dict[str, int]:
        from trader.data.bars_cache import STATS

        return dict(STATS)

    @reg.register("risk_status", kind="query", summary="Kill switches, breaker halts, and limits.")
    async def risk_status() -> dict[str, Any]:
        from dataclasses import asdict

        from trader.ops.config import get_settings
        from trader.risk.breakers import BreakerStore
        from trader.risk.limits import limits_from_settings

        assert db is not None, "risk_status requires a store"
        state = await BreakerStore(db).load()
        settings = get_settings()
        return {
            "trading_paused": state.trading_paused,
            "pause_reason": state.pause_reason,
            "drift_detected": state.drift_detected,
            "halted_reason": state.halted_reason,
            "blocked_reason": state.blocked_reason,
            "day_session": state.day_session.isoformat() if state.day_session else None,
            "day_start_equity": state.day_start_equity,
            "week_high_equity": state.week_high_equity,
            "live_enabled": settings.alpaca_live_enabled,
            "limits": asdict(limits_from_settings(settings)),
        }

    @reg.register("approvals", kind="query", summary="Order intents awaiting a human.")
    async def approvals() -> list[dict[str, Any]]:
        from trader.risk.approvals import ApprovalService

        assert db is not None, "approvals requires a store"
        rows = await ApprovalService(db).pending()
        return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]

    async def _redeem_approved(approval_id: int, resolved_by: str) -> dict[str, Any]:
        """Fresh price, TTL, tolerance, then the guard. The executor sees only executable()."""
        import math
        from datetime import UTC, datetime

        from trader.core.symbols import default_registry
        from trader.data.base import MarketDataError
        from trader.loop.runner import count_trades_today
        from trader.risk.approvals import payload_to_intent
        from trader.risk.guard import AccountState, RiskDenied
        from trader.store.repos import ApprovalsRepo, PositionsRepo

        if runner is None or getattr(runner, "executor", None) is None:
            return {"redeemed": False, "reason": "no executor in this build"}
        guard = getattr(runner, "guard", None)
        market_data = getattr(runner, "market_data", None)
        if guard is None or market_data is None:
            return {"redeemed": False, "reason": "no risk guard in this build"}
        equity = getattr(runner, "equity", None)
        if not isinstance(equity, (int, float)) or isinstance(equity, bool) or equity <= 0:
            return {"redeemed": False, "reason": "account equity unavailable"}

        assert db is not None
        row = await ApprovalsRepo(db).get(approval_id)
        if row is None:
            return {"redeemed": False, "reason": "approval not found"}
        try:
            parked = payload_to_intent(row["order_intent"])
            symbol = default_registry().get(parked.symbol)
        except (KeyError, TypeError, ValueError) as exc:
            return {"redeemed": False, "reason": f"unreadable parked intent: {exc}"}

        try:
            quote = await market_data.get_latest_quote(symbol)
            price = float(quote.mid)
        except (MarketDataError, AttributeError, TypeError, ValueError) as exc:
            return {"redeemed": False, "reason": f"no fresh price: {exc}"}
        if not math.isfinite(price) or price <= 0:
            return {"redeemed": False, "reason": "no fresh price"}

        limits = guard.limits
        service_redeem = await guard.approvals.redeem(
            approval_id,
            current_price=price,
            ttl_seconds=limits.approval_ttl_seconds,
            price_tolerance_frac=limits.approval_price_tolerance_frac,
            now=datetime.now(tz=UTC),
        )
        if not service_redeem.ok or service_redeem.intent is None:
            return {"redeemed": False, "reason": service_redeem.reason or "not redeemable"}

        now = datetime.now(tz=UTC)
        positions = {r["symbol"]: float(r["qty"]) for r in await PositionsRepo(db).all()}
        outcome = await guard.evaluate(
            service_redeem.intent,
            symbol,
            price=price,
            account=AccountState(
                equity=float(equity),
                positions=positions,
                # Same count the wakeup path uses: a redeemed open is still a trade today.
                trades_today=await count_trades_today(db, symbol, now),
            ),
            now=now,
            live=False,
            actor=resolved_by,
            decision_id=row["decision_id"],
            shadow=False,
            human_approved=True,
        )
        if not outcome.approved:
            return {"redeemed": False, "reason": outcome.reason or "risk chain denied the intent"}
        try:
            # execute() calls outcome.executable() — the only door to the broker.
            execution = await runner.executor.execute(
                outcome, symbol, price=price, decision_id=row["decision_id"]
            )
        except RiskDenied as exc:
            return {"redeemed": False, "reason": str(exc)}
        return {"redeemed": bool(execution.submitted), "reason": execution.reason or ""}

    @reg.register(
        "resolve_approval",
        kind="command",
        summary="Approve or deny a parked order intent (HITL queue; operator-only).",
    )
    async def resolve_approval(
        approval_id: int, approved: bool, resolved_by: str = "operator"
    ) -> dict[str, Any]:
        from trader.risk.approvals import ApprovalService

        assert db is not None, "resolve_approval requires a store"
        resolved = await ApprovalService(db).resolve(
            approval_id, approved=approved, resolved_by=resolved_by
        )
        status = ("approved" if approved else "denied") if resolved else "not pending"
        out: dict[str, Any] = {"resolved": resolved, "id": approval_id, "status": status}
        if resolved and approved:
            out.update(await _redeem_approved(approval_id, resolved_by))
        payload: dict[str, Any] = {
            "approval_id": approval_id,
            "status": status,
            "resolved_by": resolved_by,
        }
        if "redeemed" in out:
            payload["redeemed"] = out["redeemed"]
            payload["reason"] = out.get("reason", "")
        await _audit_command("resolve_approval", payload)
        return out

    # --- decide loop ------------------------------------------------------

    @reg.register("decisions", kind="query", summary="Recent journaled decisions.")
    async def decisions(limit: int = 20) -> list[dict[str, Any]]:
        from trader.loop.journal import Journal

        assert db is not None, "decisions requires a store"
        return await Journal(db).recent(min(limit, 200))

    @reg.register("decision", kind="query", summary="One journaled decision by id.")
    async def decision(decision_id: int) -> dict[str, Any] | None:
        from trader.loop.journal import Journal

        assert db is not None, "decision requires a store"
        return await Journal(db).get(decision_id)

    @reg.register("loop_status", kind="query", summary="Decide-loop counters and mode.")
    async def loop_status() -> dict[str, Any]:
        from trader.ops.config import get_settings

        settings = get_settings()
        if runner is None:
            return {"running": False, "shadow": settings.shadow_mode, "stats": None}
        return {
            "running": True,
            "shadow": runner.shadow,
            "shadow_symbols": sorted(runner.shadow_symbols),
            "stats": runner.stats.as_dict(),
        }

    @reg.register(
        "wake_trader", kind="command", summary="Force a decide pass now (ignores cooldowns)."
    )
    async def wake_trader() -> dict[str, Any]:
        if runner is None:
            return {"woken": False, "reason": "no decide loop in this build"}
        results = await runner.tick(force=True)
        await _audit_command("wake_trader", {"wakeups": len(results)})
        return {"woken": True, "results": [r.as_dict() for r in results]}

    @reg.register("pause", kind="command", summary="Pause trading (kill switch).")
    async def pause(reason: str = "operator") -> dict[str, Any]:
        from trader.risk.breakers import BreakerStore

        assert db is not None, "pause requires a store"
        await BreakerStore(db).pause(reason)
        await _audit_command("pause", {"reason": reason})
        return {"trading_paused": True, "reason": reason}

    @reg.register("resume", kind="command", summary="Clear the operator pause.")
    async def resume() -> dict[str, Any]:
        from trader.risk.breakers import BreakerStore

        assert db is not None, "resume requires a store"
        await BreakerStore(db).unpause()
        await _audit_command("resume", {})
        return {"trading_paused": False}

    @reg.register(
        "resume_breaker",
        kind="command",
        summary="Clear a breaker halt and re-baseline its windows.",
    )
    async def resume_breaker(equity: float | None = None) -> dict[str, Any]:
        from trader.ops.config import get_settings
        from trader.risk.breakers import BreakerStore

        assert db is not None, "resume_breaker requires a store"
        if equity is not None:
            baseline = equity
        else:
            # The runner's equity is the last broker read in a live build (the shadow
            # constant only before any read landed); store-only builds keep the constant.
            live = getattr(runner, "equity", None)
            baseline = (
                float(live)
                if isinstance(live, (int, float)) and not isinstance(live, bool) and live > 0
                else get_settings().shadow_equity
            )
        await BreakerStore(db).resume(equity=baseline)
        await _audit_command("resume_breaker", {"rebaselined_to": baseline})
        return {"halted_reason": None, "rebaselined_to": baseline}

    # --- broker / ledger --------------------------------------------------

    @reg.register("positions", kind="query", summary="Open positions in the local ledger.")
    async def positions() -> list[dict[str, Any]]:
        from trader.store.repos import PositionsRepo

        assert db is not None, "positions requires a store"
        rows = await PositionsRepo(db).all()
        return [
            {**r, "qty": float(r["qty"]), "updated_at": r["updated_at"].isoformat()} for r in rows
        ]

    @reg.register(
        "usage",
        kind="query",
        summary="LLM usage_daily rollup (requests, tokens, USD) for a day or recent window.",
    )
    async def usage(
        days: int = 14, role: str | None = None, day: str | None = None
    ) -> dict[str, Any]:
        from datetime import UTC, date, datetime, timedelta

        from trader.store.repos import UsageRepo

        assert db is not None, "usage requires a store"
        if day:
            try:
                end = date.fromisoformat(day)
            except ValueError as exc:
                raise ValueError("day must be YYYY-MM-DD") from exc
            start = end
        else:
            window = min(max(int(days), 1), 366)
            # Rows are keyed by UTC date (UsageMeter). A local date hides the
            # current UTC day for the hours this box is behind UTC.
            end = datetime.now(tz=UTC).date()
            start = end - timedelta(days=window - 1)
        rows = await UsageRepo(db).between(start, end, role=role or None)
        shaped = [
            {
                "day": r["day"].isoformat(),
                "role": r["role"],
                "provider": r["provider"],
                "model": r["model"],
                "requests": int(r["requests"]),
                "prompt_tokens": int(r["prompt_tokens"]),
                "completion_tokens": int(r["completion_tokens"]),
                "cost_usd": float(r["cost_usd"]),
            }
            for r in rows
        ]
        return {
            "rows": shaped,
            "totals": {
                "requests": sum(r["requests"] for r in shaped),
                "prompt_tokens": sum(r["prompt_tokens"] for r in shaped),
                "completion_tokens": sum(r["completion_tokens"] for r in shaped),
                "cost_usd": round(sum(r["cost_usd"] for r in shaped), 6),
            },
        }

    @reg.register("orders", kind="query", summary="Recent orders, newest first.")
    async def orders(limit: int = 50) -> list[dict[str, Any]]:
        assert db is not None, "orders requires a store"
        rows = await db.fetch_all(
            "SELECT * FROM orders ORDER BY created_at DESC, id DESC LIMIT %s", (min(limit, 200),)
        )
        return [
            {
                **r,
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
            }
            for r in rows
        ]

    @reg.register(
        "reconcile", kind="command", summary="Compare the ledger to the broker; drift pauses."
    )
    async def reconcile() -> dict[str, Any]:
        if runner is None or runner.executor is None:
            return {"reconciled": False, "reason": "no broker in this build"}
        result = await runner.executor.reconcile_positions()
        await _audit_command("reconcile", result)
        return result

    @reg.register(
        "adopt_venue_positions",
        kind="command",
        summary="Rewrite the local ledger to venue truth and clear the drift flag"
        " (confirm='ADOPT' required; operator-only, after investigating the drift).",
    )
    async def adopt_venue_positions(confirm: str = "") -> dict[str, Any]:
        if runner is None or runner.executor is None:
            return {"adopted": False, "reason": "no broker in this build"}
        if confirm != "ADOPT":
            return {"adopted": False, "reason": "confirm must be exactly 'ADOPT'"}
        count = await runner.executor.adopt_venue_positions()
        await _audit_command("adopt_venue_positions", {"positions": count})
        return {"adopted": True, "positions": count}

    @reg.register(
        "flatten_all",
        kind="command",
        summary="Close every position (confirm='FLATTEN' required).",
    )
    async def flatten_all(confirm: str = "") -> dict[str, Any]:
        if runner is None or runner.executor is None:
            return {"flattened": False, "reason": "no broker in this build"}
        result = await runner.executor.flatten_all(confirm=confirm)
        await _audit_command("flatten_all", result)
        return result

    # --- research desk (M4 part 1) ------------------------------------------

    def _note_out(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "created_at": row["created_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "authored_at": row["authored_at"].isoformat() if row["authored_at"] else None,
        }

    @reg.register(
        "research_notes",
        kind="query",
        summary="Research notes — default view is what compilation sees; flags widen it.",
    )
    async def research_notes(
        include_stale: bool = False, include_retired: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        from trader.research.notes import ResearchDesk

        assert db is not None, "research_notes requires a store"
        rows = await ResearchDesk(db).list(
            include_stale=include_stale, include_retired=include_retired, limit=min(limit, 500)
        )
        return [_note_out(r) for r in rows]

    @reg.register(
        "submit_research_note",
        kind="command",
        summary="Capture an immutable markdown research note (auto-stales unless pinned;"
        " authored_at = when the research was created, ISO-8601 — freshness anchors on it).",
    )
    async def submit_research_note(
        title: str,
        body: str,
        tags: list[str] | None = None,
        asset_classes: list[str] | None = None,
        symbols: list[str] | None = None,
        source: str | None = None,
        pinned: bool = False,
        stale_days: int | None = None,
        authored_at: str | None = None,
    ) -> dict[str, Any]:
        from datetime import datetime

        from trader.research.notes import ResearchDesk

        assert db is not None, "submit_research_note requires a store"
        # The desk writes the audit row itself (one capture = one research-category event).
        row = await ResearchDesk(db).submit(
            title=title,
            body=body,
            tags=tags,
            asset_classes=asset_classes,
            symbols=symbols,
            source=source,
            pinned=pinned,
            stale_days=stale_days,
            authored_at=datetime.fromisoformat(authored_at) if authored_at else None,
        )
        if compiler is not None:
            compiler.mark_dirty()  # debounced Macro View recompile
        if briefs is not None:
            briefs.mark_dirty()  # debounced per-candidate brief recompile
        if mirror is not None:
            await mirror.export_quietly()
        return _note_out(row)

    @reg.register("retire_note", kind="command", summary="Explicitly retire a research note by id.")
    async def retire_note(note_id: int) -> dict[str, Any]:
        from trader.research.notes import ResearchDesk

        assert db is not None, "retire_note requires a store"
        retired = await ResearchDesk(db).retire(note_id)
        if retired and compiler is not None:
            compiler.mark_dirty()
        if retired and briefs is not None:
            briefs.mark_dirty()
        if retired and mirror is not None:
            await mirror.export_quietly()
        return {"retired": retired, "id": note_id}

    def _view_out(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {**row, "created_at": row["created_at"].isoformat()}

    @reg.register(
        "macro_view",
        kind="query",
        summary="The current compiled Macro View (advisory digest of operator research).",
    )
    async def macro_view(version: int | None = None) -> dict[str, Any] | None:
        from trader.store.repos import MacroViewsRepo

        assert db is not None, "macro_view requires a store"
        repo = MacroViewsRepo(db)
        row = await (repo.current() if version is None else repo.get(version))
        return _view_out(row)

    @reg.register(
        "compile_macro_view",
        kind="command",
        summary="Recompile the Macro View from live notes now (skips the debounce).",
    )
    async def compile_macro_view() -> dict[str, Any] | None:
        if compiler is None:
            return {"compiled": False, "reason": "no compiler in this build"}
        row = await compiler.compile_now()  # its on_compiled hook refreshes the mirror
        if row is None:
            return {"compiled": False, "reason": "no live notes"}
        return {"compiled": True, "view": _view_out(row)}

    @reg.register(
        "export_research_mirror",
        kind="command",
        summary="Rewrite the one-way markdown mirror from the store now.",
    )
    async def export_research_mirror() -> dict[str, Any]:
        if mirror is None:
            return {"exported": False, "reason": "no mirror in this build"}
        return {"exported": True, **await mirror.export()}

    def _brief_out(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {**row, "ts": row["ts"].isoformat()}

    @reg.register(
        "brief",
        kind="query",
        summary="The current (or a specific) per-candidate research brief (advisory).",
    )
    async def brief(symbol: str | None = None, version: int | None = None) -> dict[str, Any] | None:
        from trader.store.repos import BriefsRepo

        assert db is not None, "brief requires a store"
        repo = BriefsRepo(db)
        row = await (repo.get(version) if version is not None else repo.current(symbol or ""))
        return _brief_out(row)

    @reg.register(
        "compile_brief",
        kind="command",
        summary="Compile one symbol's brief from store inputs now (skips the debounce).",
    )
    async def compile_brief(symbol: str) -> dict[str, Any]:
        if briefs is None:
            return {"compiled": False, "reason": "no brief compiler in this build"}
        row = await briefs.compile_now(symbol)
        if row is None:
            return {"compiled": False, "reason": "no symbol-scoped inputs (or output uncited)"}
        return {"compiled": True, "brief": _brief_out(row)}

    # --- x sentiment (M4 part 4) ---------------------------------------------

    @reg.register(
        "sentiment",
        kind="query",
        summary="Journaled X sentiment polls, newest first (scored and unavailable).",
    )
    async def sentiment_history(symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from trader.store.repos import SentimentRepo

        assert db is not None, "sentiment requires a store"
        rows = await SentimentRepo(db).recent(symbol, min(limit, 200))
        return [
            {
                **r,
                "ts": r["ts"].isoformat(),
                "score": float(r["score"]) if r["score"] is not None else None,
            }
            for r in rows
        ]

    @reg.register(
        "fetch_x_sentiment",
        kind="command",
        summary="Poll X sentiment for one symbol now (server-side x_search; capped daily).",
    )
    async def fetch_x_sentiment(symbol: str) -> dict[str, Any]:
        from trader.core.symbols import default_registry

        if sentiment is None:
            return {"status": "unavailable", "reason": "no xAI credential in this build"}
        return await sentiment.get_x_sentiment(default_registry().get(symbol))

    # --- replay eval (M5) ----------------------------------------------------

    def _run_out(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "created_at": row["created_at"].isoformat(),
            "start_ts": row["start_ts"].isoformat(),
            "end_ts": row["end_ts"].isoformat(),
        }

    @reg.register(
        "run_replay_eval",
        kind="command",
        summary="Replay the decide stage over a historical window (background; offline only).",
    )
    async def run_replay_eval(
        symbol: str, start: str, end: str, label: str = "replay", desk: bool = False
    ) -> dict[str, Any]:
        import asyncio
        from datetime import datetime

        from trader.core.symbols import default_registry

        if evaluator is None:
            return {"run_id": None, "status": "unavailable", "reason": "no evaluator in build"}
        sym = default_registry().get(symbol)
        run_id = await evaluator.create_run(
            sym,
            datetime.fromisoformat(start),
            datetime.fromisoformat(end),
            label=label,
            config={"desk": desk},
        )
        asyncio.create_task(evaluator.execute(run_id, sym), name=f"replay-eval-{run_id}")
        await _audit_command(
            "run_replay_eval", {"run_id": run_id, "symbol": symbol, "label": label, "desk": desk}
        )
        return {"run_id": run_id, "status": "running"}

    @reg.register("eval_runs", kind="query", summary="Replay eval runs, newest first.")
    async def eval_runs(limit: int = 50) -> list[dict[str, Any]]:
        from trader.store.repos import EvalRepo

        assert db is not None, "eval_runs requires a store"
        return [_run_out(r) for r in await EvalRepo(db).runs(min(limit, 200))]

    @reg.register("eval_run", kind="query", summary="One replay eval run with step summaries.")
    async def eval_run(run_id: int) -> dict[str, Any] | None:
        from trader.store.repos import EvalRepo

        assert db is not None, "eval_run requires a store"
        repo = EvalRepo(db)
        row = await repo.get_run(run_id)
        if row is None:
            return None
        steps = [
            {
                **s,
                "ts": s["ts"].isoformat(),
                "price": float(s["price"]) if s["price"] is not None else None,
            }
            for s in await repo.steps_summary(run_id)
        ]
        return {**_run_out(row), "steps": steps}

    async def _audit_command(action: str, payload: dict[str, Any]) -> None:
        """Commands are state-changing: every one leaves an append-only trace."""
        from trader.store.audit import audit as write_audit

        if db is not None:
            await write_audit(db, actor="surface", category="ops", action=action, payload=payload)

    return reg
