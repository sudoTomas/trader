"""Grok desk seats — box-local research sweeps that feed the Research Desk (#9, #20).

A seat is a stateless research function: a prompt + tool scope run once per timer tick as
a **standalone process** (`trader seat run <seat>`), never inside ``trader-api``. It calls
xAI's ``/v1/responses`` with ``x_search`` + ``web_search``, passes the candidate notes
through local deterministic gates (grounding, dedupe, size, cap) and submits the survivors
to the trader over HTTP with the capture-scoped key. The existing Macro View compiler is
the synthesizer; there is no Grok-side compiled view (design:
docs/research/2026-09-16-grok-bots-macro-team.md, accepted 2026-09-17).
"""

from __future__ import annotations

from trader.seats.crypto_flows import CRYPTO_FLOWS
from trader.seats.runner import Seat

SEATS: dict[str, Seat] = {CRYPTO_FLOWS.name: CRYPTO_FLOWS}

__all__ = ["SEATS", "Seat"]
