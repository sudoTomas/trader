"""Risk limits — every number the chain enforces, in one config-driven place.

Limits are CODE and CONFIG, never prompt text (docs/architecture.md, principle 1). No
field here may ever be populated from LLM output.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from trader.ops.config import Settings


@dataclass(frozen=True)
class RiskLimits:
    # Sizing
    max_position_frac: float = 0.10  # per-symbol cap, fraction of equity
    min_notional_usd: float = 5.0  # below this an order is not worth placing
    # Kelly cap: assumptions are configured, volatility is measured.
    kelly_fraction: float = 0.25  # quarter Kelly
    kelly_max_bet_frac: float = 0.20
    kelly_win_prob: float = 0.55
    kelly_win_loss_ratio: float = 1.5
    kelly_baseline_vol: float = 0.02  # per-bar realized vol considered "normal"
    # Throughput
    max_concurrent_positions: int = 5
    max_trades_per_day: int = 20
    # Breakers
    daily_loss_limit_frac: float = 0.03  # of the session-day starting equity
    weekly_drawdown_limit_frac: float = 0.10  # off the rolling equity high-water
    # Human-in-the-loop. Opens above the cap clamp down to it and execute unattended.
    # park_above_usd, when set, must sit strictly above that cap: only then do larger
    # opens park for a human. None disables parking.
    approval_notional_usd: float = 1000.0
    park_above_usd: float | None = None
    approval_price_tolerance_frac: float = 0.01  # see docs/lessons.md#approval-price-tolerance
    approval_ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        if not 0 < self.max_position_frac <= 1:
            raise ValueError("max_position_frac must be within (0, 1]")
        if self.min_notional_usd <= 0:
            raise ValueError("min_notional_usd must be positive")
        if self.max_concurrent_positions < 1 or self.max_trades_per_day < 1:
            raise ValueError("throughput limits must be >= 1")
        for name in ("daily_loss_limit_frac", "weekly_drawdown_limit_frac"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be within (0, 1)")
        if self.park_above_usd is not None:
            if not math.isfinite(self.park_above_usd) or self.park_above_usd <= 0:
                raise ValueError("park_above_usd must be a positive finite number when set")
            if self.park_above_usd <= self.approval_notional_usd:
                raise ValueError("park_above_usd must exceed approval_notional_usd")


def limits_from_settings(settings: Settings) -> RiskLimits:
    """Build the limit set from env config — the only supported way to change a limit."""
    return RiskLimits(
        max_position_frac=settings.risk_max_position_frac,
        min_notional_usd=settings.risk_min_notional_usd,
        max_concurrent_positions=settings.risk_max_concurrent_positions,
        max_trades_per_day=settings.risk_max_trades_per_day,
        daily_loss_limit_frac=settings.risk_daily_loss_limit_frac,
        weekly_drawdown_limit_frac=settings.risk_weekly_drawdown_limit_frac,
        approval_notional_usd=settings.risk_approval_notional_usd,
        park_above_usd=_park_above(
            settings.risk_park_above_usd, settings.risk_approval_notional_usd
        ),
    )


def _park_above(raw: float, approval_notional_usd: float) -> float | None:
    """``0`` disables the band. A negative value, or one that does not sit above the cap, raises."""
    if raw == 0:
        return None
    if raw < 0:
        raise ValueError("risk_park_above_usd must be >= 0")
    if raw <= approval_notional_usd:
        raise ValueError("risk_park_above_usd must exceed risk_approval_notional_usd")
    return raw
