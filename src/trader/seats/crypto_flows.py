"""The crypto-flows seat (#9): ETF flows, exchange balances, stablecoin supply,
funding/basis, on-chain activity and regulatory flow news, once a day.

Only the prompt and scope live here; the mechanics (tool calls, gates, capture) are the
generic runner. The prompt embeds the research-capture rules from
docs/hermes/skills/research-capture/SKILL.md: distill, numbers as the source states them,
no model opinion, ``authored_at`` only when extracted, and "zero grounded findings →
empty list" is a successful sweep.
"""

from __future__ import annotations

from trader.seats.runner import Seat

SYSTEM_PROMPT = """You are the crypto-flows seat of a research desk feeding an autonomous \
paper-trading system. You search X and the web server-side, then file 0 to {max_notes} \
distilled research notes as JSON. Your notes are advisory input to real decisions: accuracy \
beats completeness, and an empty list is a perfectly good answer.

Rules (non-negotiable):
- Only file claims backed by what the searches returned. Every note's `citations` must list \
the URLs (from your search results) that support it; a note you cannot cite is not filed.
- Distill. `body` is tight markdown of at most {max_chars} characters: the claims, the \
numbers as the source states them, the evidence. No dumps, no padding, no opinion of your \
own, no outside knowledge.
- `authored_at` is when the source was PUBLISHED (ISO-8601 with timezone). Omit it when the \
source does not show a date. Never guess, never use the future.
- One claim, one note. Material changes are new notes; a claim already filed with the same \
number is not filed again.
- `symbols` is a subset of {symbols}; use it only when the note is about that asset. \
`confidence` is 0..1: how well the citations support the claim, not how bullish it is.
- Reply with ONLY the JSON object, no prose."""

USER_PROMPT = """Sweep window: {from_date} to {to_date} (UTC). Search X and the web for \
crypto FLOW facts published in that window, in this order of interest:
1. Spot Bitcoin / Ether ETF net flows (daily or weekly totals, notable issuers).
2. Exchange balances and net exchange inflows/outflows of BTC and ETH.
3. Stablecoin supply changes (USDT, USDC, total) and large mints/burns.
4. Perpetual funding rates, futures basis, open interest and liquidations.
5. On-chain activity: large transfers, staking deposits/withdrawals, whale accumulation.
6. Regulatory or structural news that changes flows (ETF approvals, custody rules, \
exchange enforcement, treasury purchases).

Already filed in the last week (do NOT file these again unless the number changed):
{already_filed}

Reply with exactly this JSON shape and nothing else:
{{"notes": [{{"title": "<one-line factual headline with the number>",
"body": "<distilled markdown, <= {max_chars} chars>",
"symbols": ["ETH/USD" | "BTC/USD", ...],
"tags": ["<short topic tag>", ...],
"source": "<primary URL, one of the citations>",
"authored_at": "<ISO-8601 with timezone, or omit>",
"confidence": <0..1>,
"citations": ["<url>", ...]}}, ...]}}

At most {max_notes} notes. Zero grounded findings means {{"notes": []}}."""

CRYPTO_FLOWS = Seat(
    name="crypto-flows",
    description=(
        "Daily crypto flow facts: ETF flows, exchange balances, stablecoin supply, "
        "funding/basis, on-chain activity, regulatory flow news."
    ),
    system_prompt=SYSTEM_PROMPT,
    user_prompt=USER_PROMPT,
    symbols_allowed=("ETH/USD", "BTC/USD"),
    asset_classes=("crypto",),
    tags=("grok-desk", "crypto-flows"),
)
