"""RiskGuard chain — exhaustive per-check unit tests over the pure evaluator.

No DB here: ``evaluate_chain`` is fully determined by its context, which is the point —
the money-path chokepoint is deterministic and replayable.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from trader.core.symbols import AssetClass, default_registry, make_symbol
from trader.core.types import OrderIntent, Side
from trader.risk.breakers import RiskState
from trader.risk.guard import (
    AccountState,
    RiskContext,
    RiskDenied,
    RiskOutcome,
    evaluate_chain,
)
from trader.risk.limits import RiskLimits

BTC = default_registry().get("BTC/USD")
SPY = make_symbol("SPY", AssetClass.US_EQUITY)
NOSHORT = make_symbol("XYZ", AssetClass.US_EQUITY, shortable=False)

# Wednesday 15:00 UTC = 11:00 ET — NYSE open, and crypto is always open.
NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 8, 15, 15, 0, tzinfo=UTC)

# Configured edge (0.55 @ 1.5) → quarter Kelly = 6.25% of equity at baseline vol.
# max_position_frac is set below that so the position cap binds first; the Kelly test
# raises it so Kelly binds instead. Approval is effectively off unless a test opts in.
LIMITS = RiskLimits(
    max_position_frac=0.05,
    min_notional_usd=5.0,
    max_concurrent_positions=3,
    max_trades_per_day=5,
    approval_notional_usd=100_000.0,
)
KELLY_BINDS = replace(LIMITS, max_position_frac=0.50)
APPROVAL_LIMITS = replace(LIMITS, approval_notional_usd=1_000.0)


def ctx(**overrides) -> RiskContext:
    base = {
        "intent": OrderIntent("BTC/USD", Side.BUY, notional=500.0),
        "symbol": BTC,
        "price": 60000.0,
        "account": AccountState(equity=100_000.0),
        "state": RiskState(),
        "limits": LIMITS,
        "now": NOW,
        "realized_vol": 0.02,  # == baseline → no vol scaling
    }
    return RiskContext(**{**base, **overrides})


def checks(outcome: RiskOutcome) -> list[str]:
    return [v.check for v in outcome.verdicts]


def denied_by(outcome: RiskOutcome) -> str:
    assert not outcome.approved
    return outcome.verdicts[-1].check


# --- 1. input sanity (fail closed) -----------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"price": 0.0},
        {"price": -1.0},
        {"price": math.nan},
        {"price": math.inf},
        {"account": AccountState(equity=0.0)},
        {"account": AccountState(equity=math.nan)},
        {"intent": OrderIntent("BTC/USD", Side.BUY, qty=math.nan)},
        {"intent": OrderIntent("BTC/USD", Side.BUY, notional=math.inf)},
        {"intent": OrderIntent("BTC/USD", Side.BUY, notional=100.0, limit_price=math.nan)},
    ],
)
def test_unreadable_input_is_denied_never_allowed(overrides):
    outcome = evaluate_chain(ctx(**overrides))
    assert denied_by(outcome) == "input_sanity"
    assert outcome.intent is None


# --- 2. paper gate ----------------------------------------------------------


def test_live_routing_denied_unless_explicitly_enabled():
    assert denied_by(evaluate_chain(ctx(live=True, live_enabled=False))) == "paper_gate"
    assert evaluate_chain(ctx(live=True, live_enabled=True)).approved
    assert evaluate_chain(ctx(live=False, live_enabled=False)).approved  # paper is the default


# --- 3. kill switches -------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        RiskState(trading_paused=True, pause_reason="operator"),
        RiskState(drift_detected=True),
    ],
)
def test_kill_switches_block_everything_including_exits(state):
    assert denied_by(evaluate_chain(ctx(state=state))) == "kill_switch"
    # Even a risk-reducing exit is refused: under pause/drift we cannot trust the book.
    exit_ctx = ctx(
        state=state,
        intent=OrderIntent("BTC/USD", Side.SELL, qty=0.01),
        account=AccountState(equity=100_000.0, positions={"BTC/USD": 0.5}),
    )
    assert denied_by(evaluate_chain(exit_ctx)) == "kill_switch"


# --- 4. per-asset-class policy ---------------------------------------------


def test_crypto_is_long_only():
    naked_short = ctx(intent=OrderIntent("BTC/USD", Side.SELL, notional=500.0))
    assert denied_by(evaluate_chain(naked_short)) == "class_policy"

    closing = ctx(
        intent=OrderIntent("BTC/USD", Side.SELL, qty=0.001),
        account=AccountState(equity=100_000.0, positions={"BTC/USD": 0.5}),
    )
    assert evaluate_chain(closing).approved  # selling to close a long is fine


def test_equity_shortability_respected():
    shortable = ctx(symbol=SPY, intent=OrderIntent("SPY", Side.SELL, notional=500.0), price=600.0)
    assert evaluate_chain(shortable).approved

    unshortable = ctx(
        symbol=NOSHORT, intent=OrderIntent("XYZ", Side.SELL, notional=500.0), price=50.0
    )
    assert denied_by(evaluate_chain(unshortable)) == "class_policy"


def test_venue_must_be_open():
    closed = ctx(symbol=SPY, intent=OrderIntent("SPY", Side.BUY, notional=500.0), now=SATURDAY)
    assert denied_by(evaluate_chain(closed)) == "class_policy"
    # Crypto trades through the same instant.
    assert evaluate_chain(ctx(now=SATURDAY)).approved


# --- 5. sizing clamps -------------------------------------------------------


def test_clamped_to_max_position_fraction():
    outcome = evaluate_chain(ctx(intent=OrderIntent("BTC/USD", Side.BUY, notional=50_000.0)))
    assert outcome.approved
    assert outcome.executable().notional == pytest.approx(5_000.0)  # 5% of 100k
    assert "max_position_frac" in checks(outcome)


def test_existing_position_consumes_headroom():
    # 0.05 BTC @ 60k = 3,000 held against a 5,000 cap → 2,000 of headroom.
    partial = ctx(
        intent=OrderIntent("BTC/USD", Side.BUY, notional=9_000.0),
        account=AccountState(equity=100_000.0, positions={"BTC/USD": 0.05}),
    )
    assert evaluate_chain(partial).executable().notional == pytest.approx(2_000.0)

    full = ctx(
        intent=OrderIntent("BTC/USD", Side.BUY, notional=1_000.0),
        account=AccountState(equity=100_000.0, positions={"BTC/USD": 0.1}),
    )
    assert denied_by(evaluate_chain(full)) == "max_position_frac"


def test_kelly_cap_binds_and_scales_with_volatility():
    # With the position cap raised out of the way: quarter Kelly = 6.25% at baseline vol.
    calm = evaluate_chain(
        ctx(
            limits=KELLY_BINDS,
            intent=OrderIntent("BTC/USD", Side.BUY, notional=50_000.0),
            realized_vol=0.02,
        )
    )
    assert calm.executable().notional == pytest.approx(6_250.0)

    # Double the vol halves the Kelly cap; the clamp must bind tighter.
    wild = evaluate_chain(
        ctx(
            limits=KELLY_BINDS,
            intent=OrderIntent("BTC/USD", Side.BUY, notional=50_000.0),
            realized_vol=0.04,
        )
    )
    assert wild.executable().notional == pytest.approx(3_125.0)
    assert "kelly_cap" in checks(wild)


def test_quantity_expressed_intents_are_clamped_in_quantity():
    outcome = evaluate_chain(ctx(intent=OrderIntent("BTC/USD", Side.BUY, qty=1.0)))
    executable = outcome.executable()
    assert executable.notional is None
    assert executable.qty == pytest.approx(5_000.0 / 60_000.0)  # clamped, still a qty


def test_below_minimum_notional_is_denied():
    assert denied_by(
        evaluate_chain(ctx(intent=OrderIntent("BTC/USD", Side.BUY, notional=1.0)))
    ) == ("min_notional")
    # A clamp that lands under the floor is a deny, not a dust order.
    tiny_equity = ctx(
        intent=OrderIntent("BTC/USD", Side.BUY, notional=500.0),
        account=AccountState(equity=40.0),
    )
    assert denied_by(evaluate_chain(tiny_equity)) == "min_notional"


# --- 6. throughput ----------------------------------------------------------


def test_max_concurrent_positions():
    full_book = {"A": 1.0, "B": 1.0, "C": 1.0}
    blocked = ctx(account=AccountState(equity=100_000.0, positions=full_book))
    assert denied_by(evaluate_chain(blocked)) == "max_concurrent"

    # Adding to a symbol already held does not open a new slot.
    adding = ctx(account=AccountState(equity=100_000.0, positions={**full_book, "BTC/USD": 0.01}))
    assert evaluate_chain(adding).approved


def test_max_trades_per_day():
    spent = ctx(account=AccountState(equity=100_000.0, trades_today=5))
    assert denied_by(evaluate_chain(spent)) == "max_trades_per_day"


# --- 7. breakers ------------------------------------------------------------


def test_breaker_halts_opens_but_never_traps_an_exit():
    halted = RiskState(halted_reason="daily_loss")
    assert denied_by(evaluate_chain(ctx(state=halted))) == "breaker"

    exiting = ctx(
        state=halted,
        intent=OrderIntent("BTC/USD", Side.SELL, qty=0.01),
        account=AccountState(equity=100_000.0, positions={"BTC/USD": 0.5}),
    )
    outcome = evaluate_chain(exiting)
    assert outcome.approved
    assert "reducing_position" in checks(outcome)


# --- 8. approval threshold --------------------------------------------------


def test_oversized_intent_parks_instead_of_executing():
    outcome = evaluate_chain(
        ctx(limits=APPROVAL_LIMITS, intent=OrderIntent("BTC/USD", Side.BUY, notional=5_000.0))
    )
    assert outcome.approval_required
    assert not outcome.approved
    assert outcome.intent is not None  # sized, ready for a human to approve
    with pytest.raises(RiskDenied):
        outcome.executable()  # ...but NOT executable


# --- ordering + no-bypass ---------------------------------------------------


def test_chain_runs_in_the_documented_order():
    outcome = evaluate_chain(ctx(intent=OrderIntent("BTC/USD", Side.BUY, notional=500.0)))
    assert checks(outcome) == [
        "input_sanity",
        "paper_gate",
        "kill_switch",
        "class_policy",
        "max_position_frac",
        "kelly_cap",
        "min_notional",
        "throughput",
        "breaker",
        "approval_threshold",
    ]


DENIAL_CASES = [
    ("input_sanity", {"price": math.nan}),
    ("paper_gate", {"live": True}),
    ("kill_switch", {"state": RiskState(trading_paused=True)}),
    ("class_policy", {"intent": OrderIntent("BTC/USD", Side.SELL, notional=500.0)}),
    ("max_position_frac", {"account": AccountState(equity=100_000.0, positions={"BTC/USD": 0.2})}),
    ("min_notional", {"intent": OrderIntent("BTC/USD", Side.BUY, notional=1.0)}),
    ("max_trades_per_day", {"account": AccountState(equity=100_000.0, trades_today=99)}),
    ("breaker", {"state": RiskState(halted_reason="weekly_drawdown")}),
    (
        "approval_threshold",
        {
            "limits": APPROVAL_LIMITS,
            "intent": OrderIntent("BTC/USD", Side.BUY, notional=5_000.0),
        },
    ),
]


@pytest.mark.parametrize(("expected_check", "overrides"), DENIAL_CASES)
def test_no_denied_intent_is_ever_executable(expected_check, overrides):
    """The no-bypass property: a denial yields nothing a broker could act on."""
    outcome = evaluate_chain(ctx(**overrides))
    assert not outcome.approved
    assert denied_by(outcome) == expected_check
    with pytest.raises(RiskDenied):
        outcome.executable()


def test_approved_outcome_only_ever_shrinks_the_request():
    """Property: the chain may clamp down, never up."""
    for requested in (10.0, 500.0, 999.0):
        outcome = evaluate_chain(ctx(intent=OrderIntent("BTC/USD", Side.BUY, notional=requested)))
        if outcome.approved:
            assert outcome.executable().notional <= requested + 1e-9


def test_limits_are_config_driven_not_prompt_driven():
    """Tightening a config limit tightens the verdict, and nothing else can."""
    request = OrderIntent("BTC/USD", Side.BUY, notional=50_000.0)
    loose = evaluate_chain(ctx(intent=request)).executable().notional
    strict = (
        evaluate_chain(ctx(limits=replace(LIMITS, max_position_frac=0.01), intent=request))
        .executable()
        .notional
    )
    assert strict == pytest.approx(1_000.0)
    assert strict < loose

    # There is no channel for LLM text to reach the chain: the context carries no
    # rationale/conviction/macro-view field at all (advisory firewall).
    assert {"rationale", "conviction", "macro_view", "research"}.isdisjoint(
        RiskContext.__dataclass_fields__
    )
