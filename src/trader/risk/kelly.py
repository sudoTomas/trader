"""Fractional Kelly position sizing. Ported from v1 (``v1:risk/kelly.py``) — the maths
there was correct and property-tested; only the docstrings changed.

Inputs are CONFIG assumptions plus MEASURED volatility. LLM conviction is deliberately
not an input: sizing must never be prompt-driven (docs/architecture.md, principle 1).
"""

from __future__ import annotations


def kelly_fraction(
    win_prob: float,
    win_loss_ratio: float,
    fraction: float = 0.25,
    max_bet_frac: float = 0.20,
) -> float:
    """Fractional Kelly as a portfolio fraction, in ``[0, max_bet_frac]``.

    Zero (no bet) when the edge is non-positive or the inputs are degenerate.
    """
    if win_prob <= 0 or win_loss_ratio <= 0:
        return 0.0
    edge = win_prob * win_loss_ratio - (1 - win_prob)
    if edge <= 0:
        return 0.0
    full_kelly = edge / win_loss_ratio
    return min(full_kelly * fraction, max_bet_frac)


def vol_adjusted_kelly(
    win_prob: float,
    win_loss_ratio: float,
    current_vol: float,
    baseline_vol: float,
    fraction: float = 0.25,
    max_bet_frac: float = 0.20,
) -> float:
    """Fractional Kelly scaled inversely by realized vol vs a baseline.

    At 2x baseline vol the size halves; the scalar is clamped to ``[0.25, 2.0]`` so a
    freak vol reading can neither zero us out nor lever us up.
    """
    if current_vol <= 0 or baseline_vol <= 0:
        return 0.0
    base_kelly = kelly_fraction(win_prob, win_loss_ratio, fraction, max_bet_frac)
    if base_kelly <= 0:
        return 0.0
    vol_scalar = max(0.25, min(baseline_vol / current_vol, 2.0))
    return min(base_kelly * vol_scalar, max_bet_frac)
