"""Replay costs — what a replayed fill pays beyond the bar it marks at (#139).

Three parts, each in dollars of the fill's notional:

- **spread**: half the bid-ask spread. A real quote's own half-spread when there is one;
  otherwise a per-class fallback (equities and ETFs, crypto). Replay's quote today is the
  last close on both sides, which is no quote, so the fallback applies.
- **fee**: a flat fee on every fill, plus Alpaca's taker fee on crypto.
- **impact**: the square-root law. An order that is a fraction ``q`` of the symbol's
  average daily dollar volume moves its price by about ``Y * sigma * sqrt(q)``, where
  ``sigma`` is the daily volatility and ``Y`` a coefficient of order one (Almgren et al.
  2005; Toth, Bouchaud et al. 2011). Modelled on ai-hedge-fund's
  ``hedge_fund/brokers/costs.py`` (MIT), ``impact_coefficient * volatility *
  sqrt(participation)``. No liquidity reading = no impact.

Every knob at 0 turns its part off; the default model is free, so a harness built without
one scores as before. The live loop never reads this: its fills are the venue's.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from trader.core.symbols import AssetClass
from trader.ops.config import Settings

_BP = 1e-4


@dataclass(frozen=True)
class FillCost:
    spread: float = 0.0
    fee: float = 0.0
    impact: float = 0.0

    @property
    def total(self) -> float:
        return self.spread + self.fee + self.impact

    def as_dict(self) -> dict[str, float]:
        return {
            "spread": round(self.spread, 6),
            "fee": round(self.fee, 6),
            "impact": round(self.impact, 6),
            "total": round(self.total, 6),
        }

    def __add__(self, other: FillCost) -> FillCost:
        return FillCost(
            self.spread + other.spread, self.fee + other.fee, self.impact + other.impact
        )


@dataclass(frozen=True)
class ReplayCosts:
    half_spread_bps_equity: float = 0.0  # fallback when there is no real quote
    half_spread_bps_crypto: float = 0.0
    fee_bps: float = 0.0  # every fill
    crypto_taker_fee_bps: float = 0.0  # crypto fills, on top of fee_bps
    impact_coef: float = 0.0  # Y in Y * daily vol * sqrt(notional / ADV)

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{name} must be a finite number >= 0")

    def as_dict(self) -> dict[str, float]:
        return asdict(self)

    def fill(
        self,
        asset_class: AssetClass,
        notional: float,
        *,
        bid: float | None = None,
        ask: float | None = None,
        adv_usd: float | None = None,
        daily_vol: float | None = None,
    ) -> FillCost:
        """What one fill of ``notional`` dollars pays. ``bid``/``ask`` is the quote at the
        fill (a locked or crossed one is no quote); ``adv_usd`` and ``daily_vol`` are the
        symbol's average daily dollar volume and daily volatility, for the impact."""
        crypto = asset_class is AssetClass.CRYPTO
        fallback = self.half_spread_bps_crypto if crypto else self.half_spread_bps_equity
        spread = 0.0
        if fallback > 0:  # the class's knob at 0 turns the spread off, quote or not
            if bid is not None and ask is not None and 0 < bid < ask:
                spread = notional * (ask - bid) / (ask + bid)  # half-spread over the mid
            else:
                spread = notional * fallback * _BP
        fee_bps = self.fee_bps + (self.crypto_taker_fee_bps if crypto else 0.0)
        impact = 0.0
        if self.impact_coef > 0 and _positive(adv_usd) and _positive(daily_vol):
            assert adv_usd is not None and daily_vol is not None
            impact = notional * self.impact_coef * daily_vol * math.sqrt(notional / adv_usd)
        return FillCost(spread=spread, fee=notional * fee_bps * _BP, impact=impact)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def costs_from_settings(settings: Settings) -> ReplayCosts:
    """The configured cost model (``TRADER_REPLAY_*``)."""
    return ReplayCosts(
        half_spread_bps_equity=settings.replay_half_spread_bps_equity,
        half_spread_bps_crypto=settings.replay_half_spread_bps_crypto,
        fee_bps=settings.replay_fee_bps,
        crypto_taker_fee_bps=settings.replay_crypto_taker_fee_bps,
        impact_coef=settings.replay_impact_coef,
    )
