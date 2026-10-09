"""RiskGuard — the ordered, deterministic chokepoint. THE prime directive in code.

Every order intent, from any source (LLM, MCP tool, API caller, CLI), passes this chain
before a broker sees it. Each check returns **allow / deny(reason) / clamp(size, reason)**;
a deny short-circuits, clamps accumulate. Unreadable input is a deny, never an allow.

Order (deliberate — an audit reader should see the cheap absolute gates first):

1. input sanity  — non-finite/absurd numbers are a deny, never a silent 0
2. paper gate    — a live order is impossible unless TRADER_ALPACA_LIVE_ENABLED=true
3. kill switches — operator pause / position drift: block EVERYTHING (under drift we do
                   not know the true position, so even an exit could be wrong)
4. class policy  — crypto + commodity ETFs long-only, equity shortability, venue
                   session open
5. stop cooldown — a symbol the deterministic stop closed takes no opens for a while
6. sizing clamps — max position fraction, Kelly cap, minimum notional floor
7. throughput    — concurrent positions, trades per day
8. breakers      — daily loss, weekly drawdown: block OPENS, exits stay managed
9. approval      — opens above the unattended cap clamp down to it and execute.
                   Only an explicit park band above that cap waits for a human.
                   A clamp under the notional floor is a deny (no dust order).
10. full close / whole shares — an exit for at least 98% of the holding's value
                   closes exactly the held qty (a dollar-rounded close leaves no dust).
                   Otherwise a symbol the venue will not fill fractionally gets a share
                   count, rounded down; less than one share is a deny.

Risk-reducing intents (closing//reducing an existing position) skip 5-9: a limit that
trapped an open position would be a larger risk than the one it guards. Exits are
never clamped to the approval cap — closes must land.

**The Macro View / research is never an input here** (advisory firewall): research shapes
direction and conviction, never sizing or breakers. A test asserts this module tree never
imports ``trader.research``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import structlog

from trader.core.symbols import AssetClass, Symbol
from trader.core.types import OrderIntent, Side
from trader.risk.approvals import ApprovalService
from trader.risk.breakers import BreakerStore, RiskState
from trader.risk.kelly import vol_adjusted_kelly
from trader.risk.limits import RiskLimits
from trader.store.audit import audit
from trader.store.db import Database

log = structlog.get_logger(__name__)

ALLOW, DENY, CLAMP = "allow", "deny", "clamp"
# Float slack when a dollar size divides into shares (100.0 / 20.0 must be 5, not 4.999…).
_QTY_EPSILON = 1e-9
# An exit asking for at least this share of the holding closes all of it, by qty.
_FULL_CLOSE_FRAC = 0.98


@dataclass(frozen=True)
class Verdict:
    check: str
    action: str  # allow | deny | clamp
    reason: str = ""
    notional: float | None = None  # the working notional after this check

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "action": self.action,
            "reason": self.reason,
            "notional": self.notional,
        }


@dataclass(frozen=True)
class AccountState:
    """What the guard needs to know about the account, all locally computed."""

    equity: float
    positions: dict[str, float] = field(default_factory=dict)  # ticker -> signed qty
    trades_today: int = 0


@dataclass(frozen=True)
class RiskContext:
    intent: OrderIntent
    symbol: Symbol
    price: float
    account: AccountState
    state: RiskState
    limits: RiskLimits
    now: datetime
    realized_vol: float | None = None  # measured locally; None → no vol scaling
    live: bool = False  # caller is asking to route to a LIVE broker
    live_enabled: bool = False  # TRADER_ALPACA_LIVE_ENABLED
    human_approved: bool = False  # a human already cleared the park band; do not re-clamp it
    stopped_at: datetime | None = None  # when the deterministic stop last closed this symbol

    @property
    def position_qty(self) -> float:
        return self.account.positions.get(self.symbol.ticker, 0.0)

    @property
    def is_reducing(self) -> bool:
        """True when this intent shrinks an existing position (an exit)."""
        pos = self.position_qty
        if pos == 0:
            return False
        return (pos > 0 and self.intent.side is Side.SELL) or (
            pos < 0 and self.intent.side is Side.BUY
        )


class RiskDenied(RuntimeError):
    """Raised when code tries to execute an intent the chain did not approve."""


@dataclass(frozen=True)
class RiskOutcome:
    approved: bool
    intent: OrderIntent | None
    verdicts: list[Verdict]
    reason: str = ""
    approval_required: bool = False
    approval_id: int | None = None

    def executable(self) -> OrderIntent:
        """The ONLY sanctioned way to hand an intent to a broker.

        ``intent`` alone is not permission — a parked (approval-required) outcome carries
        a sized intent but is NOT approved. Executors must go through this method so a
        missed ``approved`` check raises instead of silently trading.
        """
        if not self.approved or self.intent is None:
            raise RiskDenied(self.reason or "risk chain denied the intent")
        return self.intent

    @property
    def verdict_dicts(self) -> list[dict[str, Any]]:
        return [v.as_dict() for v in self.verdicts]


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _requested_notional(intent: OrderIntent, price: float) -> float | None:
    if intent.notional is not None:
        return intent.notional
    if intent.qty is not None:
        return intent.qty * price
    return None


def _resize(intent: OrderIntent, notional: float, price: float) -> OrderIntent:
    """Rebuild the intent at ``notional``, preserving how it was expressed."""
    if intent.notional is not None:
        return replace(intent, notional=notional)
    return replace(intent, qty=notional / price)


# ---------------------------------------------------------------------------
# The chain. Pure: no I/O, fully determined by the context.
# ---------------------------------------------------------------------------


def evaluate_chain(ctx: RiskContext) -> RiskOutcome:
    verdicts: list[Verdict] = []

    def deny(check: str, reason: str) -> RiskOutcome:
        verdicts.append(Verdict(check, DENY, reason))
        log.warning("risk_denied", check=check, reason=reason, symbol=ctx.symbol.ticker)
        return RiskOutcome(False, None, verdicts, reason=f"{check}: {reason}")

    def allow(check: str, notional: float, reason: str = "") -> None:
        verdicts.append(Verdict(check, ALLOW, reason, notional))

    def clamp(check: str, notional: float, reason: str) -> None:
        verdicts.append(Verdict(check, CLAMP, reason, notional))

    def finish(notional: float) -> RiskOutcome:
        """Approve at ``notional``; a full exit or a whole-share venue gets a qty (step 10)."""
        requested = notional / ctx.price
        held = abs(ctx.position_qty)
        if ctx.is_reducing and requested >= held * _FULL_CLOSE_FRAC - _QTY_EPSILON:
            # (Nearly) covers the holding: close exactly what is held.
            unit = "units" if ctx.symbol.asset_class is AssetClass.CRYPTO else "shares"
            allow("full_close", held * ctx.price, f"closes the whole holding ({held:g} {unit})")
            return RiskOutcome(True, replace(ctx.intent, qty=held, notional=None), verdicts)
        if ctx.symbol.fractionable:
            return RiskOutcome(True, _resize(ctx.intent, notional, ctx.price), verdicts)
        shares = math.floor(requested + _QTY_EPSILON)
        if shares <= 0:
            return deny(
                "whole_shares",
                f"{notional:.2f} is less than one share at {ctx.price:.2f}"
                f" ({ctx.symbol.ticker} trades in whole shares only)",
            )
        value = shares * ctx.price
        if shares == requested:
            allow("whole_shares", value)
        else:
            clamp("whole_shares", value, f"rounded down to {shares:g} whole shares")
        return RiskOutcome(True, replace(ctx.intent, qty=shares, notional=None), verdicts)

    # --- 1. input sanity: fail closed on anything unreadable -----------------
    notional = _requested_notional(ctx.intent, ctx.price)
    if not _finite_positive(ctx.price):
        return deny("input_sanity", "price is missing or not a positive finite number")
    if not _finite_positive(ctx.account.equity):
        return deny("input_sanity", "account equity is missing or not positive")
    if not _finite_positive(notional):
        return deny("input_sanity", "order size is missing or not a positive finite number")
    if ctx.intent.limit_price is not None and not _finite_positive(ctx.intent.limit_price):
        return deny("input_sanity", "limit price is not a positive finite number")
    assert notional is not None  # narrowed by _finite_positive above
    allow("input_sanity", notional)

    # --- 2. paper gate -------------------------------------------------------
    if ctx.live and not ctx.live_enabled:
        return deny(
            "paper_gate", "live routing requested but TRADER_ALPACA_LIVE_ENABLED is not set"
        )
    allow("paper_gate", notional, "live" if ctx.live else "paper")

    # --- 3. kill switches (block everything, exits included) -----------------
    blocked = ctx.state.blocked_reason
    if blocked and (ctx.state.trading_paused or ctx.state.drift_detected):
        return deny("kill_switch", blocked)
    allow("kill_switch", notional)

    # --- 4. per-asset-class policy ------------------------------------------
    if not ctx.symbol.market_calendar.is_open(ctx.now):
        return deny(
            "class_policy", f"{ctx.symbol.market_calendar.name} venue is closed at {ctx.now}"
        )
    if not ctx.symbol.tradable and not ctx.is_reducing:
        return deny("class_policy", f"{ctx.symbol.ticker} is not tradable at the venue")
    opening_short = ctx.intent.side is Side.SELL and not ctx.is_reducing
    if opening_short:
        if ctx.symbol.asset_class is AssetClass.CRYPTO:
            return deny("class_policy", "crypto is long-only; a sell may only close a long")
        # Commodity ETFs deny here by class default (shortable=False); equities deny
        # here too until shortability earns its own gate.
        if not ctx.symbol.shortable:
            return deny("class_policy", f"{ctx.symbol.ticker} is not shortable")
    allow("class_policy", notional)

    if ctx.is_reducing:
        # Exits skip the remaining limits by design (see module docstring).
        allow("reducing_position", notional, "risk-reducing: sizing/throughput limits skipped")
        return finish(notional)

    # --- 5. stop cooldown (opens only) ---------------------------------------
    cooldown = timedelta(seconds=ctx.limits.stop_cooldown_seconds)
    if ctx.stopped_at is not None and ctx.now - ctx.stopped_at < cooldown:
        until = ctx.stopped_at + cooldown
        return deny(
            "stop_cooldown",
            f"stopped out at {ctx.stopped_at.isoformat()}; no opens until {until.isoformat()}",
        )
    allow("stop_cooldown", notional)

    # --- 6. sizing clamps ----------------------------------------------------
    cap = ctx.account.equity * ctx.limits.max_position_frac
    held = abs(ctx.position_qty) * ctx.price
    headroom = cap - held
    if headroom <= 0:
        return deny(
            "max_position_frac",
            f"position cap reached ({held:.2f} held vs {cap:.2f} cap)",
        )
    if notional > headroom:
        notional = headroom
        clamp("max_position_frac", notional, f"clamped to {ctx.limits.max_position_frac:.0%} cap")
    else:
        allow("max_position_frac", notional)

    kelly_frac = vol_adjusted_kelly(
        win_prob=ctx.limits.kelly_win_prob,
        win_loss_ratio=ctx.limits.kelly_win_loss_ratio,
        current_vol=ctx.realized_vol if _finite_positive(ctx.realized_vol) else 0.0,
        baseline_vol=ctx.limits.kelly_baseline_vol,
        fraction=ctx.limits.kelly_fraction,
        max_bet_frac=ctx.limits.kelly_max_bet_frac,
    )
    if kelly_frac <= 0:
        # No measurable vol → no vol scaling, but the configured Kelly cap still binds.
        from trader.risk.kelly import kelly_fraction as _kelly

        kelly_frac = _kelly(
            ctx.limits.kelly_win_prob,
            ctx.limits.kelly_win_loss_ratio,
            ctx.limits.kelly_fraction,
            ctx.limits.kelly_max_bet_frac,
        )
    kelly_cap = ctx.account.equity * kelly_frac
    if kelly_cap <= 0:
        return deny("kelly_cap", "configured edge implies no position")
    if notional > kelly_cap:
        notional = kelly_cap
        clamp("kelly_cap", notional, f"clamped to {kelly_frac:.2%} of equity")
    else:
        allow("kelly_cap", notional)

    floor = max(ctx.limits.min_notional_usd, ctx.symbol.min_notional)
    if notional < floor:
        return deny("min_notional", f"{notional:.2f} is below the {floor:.2f} floor")
    allow("min_notional", notional)

    # --- 7. throughput -------------------------------------------------------
    open_count = sum(1 for qty in ctx.account.positions.values() if qty != 0)
    if ctx.position_qty == 0 and open_count >= ctx.limits.max_concurrent_positions:
        return deny(
            "max_concurrent",
            f"{open_count} open positions >= {ctx.limits.max_concurrent_positions}",
        )
    if ctx.account.trades_today >= ctx.limits.max_trades_per_day:
        return deny(
            "max_trades_per_day",
            f"{ctx.account.trades_today} trades today >= {ctx.limits.max_trades_per_day}",
        )
    allow("throughput", notional)

    # --- 8. breakers (opens only; exits already returned above) --------------
    if ctx.state.halted_reason:
        return deny("breaker", f"halted: {ctx.state.halted_reason} (exits still allowed)")
    allow("breaker", notional)

    # --- 9. approval threshold ----------------------------------------------
    # Unattended opens clamp to the cap. Parking is opt-in, and only above that cap.
    # Exits returned at the reducing-position check and never reach this step.
    # A human approval clears THIS step only — sizing, breakers, and class policy
    # still bind, so redemption cannot trade a size the chain would not allow.
    if ctx.human_approved:
        allow(
            "approval_threshold",
            notional,
            "human-approved; unattended cap already cleared",
        )
        return finish(notional)
    threshold = ctx.limits.approval_notional_usd
    if not _finite_positive(threshold):
        return deny("approval_threshold", "approval cap is missing or not a positive finite number")
    park_above = ctx.limits.park_above_usd
    if park_above is not None and park_above > threshold and notional > park_above:
        sized = _resize(ctx.intent, notional, ctx.price)
        reason = (
            f"{notional:.2f} exceeds the {park_above:.2f} park band"
            f" (unattended cap {threshold:.2f})"
        )
        verdicts.append(Verdict("approval_threshold", DENY, reason, notional))
        return RiskOutcome(
            False,
            sized,
            verdicts,
            reason=f"approval_threshold: {reason}",
            approval_required=True,
        )
    if notional > threshold:
        floor = max(ctx.limits.min_notional_usd, ctx.symbol.min_notional)
        if threshold < floor:
            return deny(
                "min_notional",
                f"approval cap {threshold:.2f} is below the {floor:.2f} floor; refusing dust",
            )
        before = notional
        notional = threshold
        clamp(
            "approval_threshold",
            notional,
            f"clamped from {before:.2f} to the {threshold:.2f} unattended cap",
        )
    else:
        allow("approval_threshold", notional)
    return finish(notional)


# ---------------------------------------------------------------------------
# Async shell: load state, run the pure chain, park approvals, audit.
# ---------------------------------------------------------------------------


class RiskGuard:
    """The only sanctioned path from an intent to an executable order."""

    def __init__(self, db: Database, limits: RiskLimits, *, live_enabled: bool = False) -> None:
        self._db = db
        self._limits = limits
        self._live_enabled = live_enabled
        self.breakers = BreakerStore(db)
        self.approvals = ApprovalService(db)

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    async def _last_stop(self, ticker: str, now: datetime) -> datetime | None:
        """When the deterministic stop last sent a close for ``ticker``. A stop that was
        blocked or ran in shadow sent nothing, so it starts no cooldown."""
        row = await self._db.fetch_one(
            "SELECT max(ts) AS ts FROM decisions WHERE symbol = %s AND trigger = 'stop_loss'"
            " AND status = 'executed' AND ts <= %s",
            (ticker, now),
        )
        return row["ts"] if row else None

    async def evaluate(
        self,
        intent: OrderIntent,
        symbol: Symbol,
        *,
        price: float,
        account: AccountState,
        now: datetime,
        realized_vol: float | None = None,
        live: bool = False,
        actor: str = "loop",
        decision_id: int | None = None,
        shadow: bool = False,
        human_approved: bool = False,
    ) -> RiskOutcome:
        """Run the chain. Returns the (possibly clamped) intent only when approved.

        ``shadow`` runs the identical chain but parks nothing: a human must never be
        asked to approve an order that was never going to be submitted. The verdicts are
        still produced and audited, which is the point of shadow mode.
        """
        state = await self.breakers.observe(
            equity=account.equity,
            session_day=symbol.market_calendar.session_day(now),
            daily_loss_limit_frac=self._limits.daily_loss_limit_frac,
            weekly_drawdown_limit_frac=self._limits.weekly_drawdown_limit_frac,
        )
        outcome = evaluate_chain(
            RiskContext(
                intent=intent,
                symbol=symbol,
                price=price,
                account=account,
                state=state,
                limits=self._limits,
                now=now,
                realized_vol=realized_vol,
                live=live,
                live_enabled=self._live_enabled,
                human_approved=human_approved,
                stopped_at=await self._last_stop(symbol.ticker, now),
            )
        )

        if outcome.approval_required and outcome.intent is not None and not shadow:
            approval_id = await self.approvals.park(
                outcome.intent, price=price, reason=outcome.reason, decision_id=decision_id
            )
            outcome = replace(outcome, approval_id=approval_id)

        await audit(
            self._db,
            actor=actor,
            category="risk",
            action="shadow_verdict" if shadow else "verdict",
            subject=symbol.ticker,
            payload={
                "approved": outcome.approved,
                "reason": outcome.reason,
                "approval_id": outcome.approval_id,
                "verdicts": outcome.verdict_dicts,
                "requested": {"qty": intent.qty, "notional": intent.notional},
                "price": price,
            },
        )
        return outcome
