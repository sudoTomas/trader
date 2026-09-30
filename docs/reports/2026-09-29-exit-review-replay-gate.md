# Exit-review lane replay gate — stage 1 for #34

**Date:** 2026-09-29/30 · **Symbol:** ETH/USD · **Window:** 2026-09-13 00:00 → 2026-09-27 00:00 UTC (336 hourly steps)
**Held constant:** desk on, Macro View on, briefs on (live config), lanes pinned per step, **seeded position** 0.377902532 ETH @ $2,647.11 (the real book on 2026-09-29) so the review lane has something to review.
**Variable:** the held-position review block in the decide prompt (`--no-exit-review` vs `--exit-review`).

| Run | Label | Errors | Actions | Critic (veto / shrink / approve) | Realized | Unrealized at end | Net (crude) |
|---|---|---|---|---|---|---|---|
| 16 | exit-review-off-v8 | 0 | 336 hold | 0 / 0 / 0 | $0.00 | +$15.68 | +$15.68 |
| 17 | exit-review-on-v8 | 0 | 332 hold, 2 open, 2 close | 7 / 1 / 3 | −$52.45 | +$85.71 | +$33.26 |

Sanity: the block appeared in 218/336 run-17 prompts (exactly the steps where a position was held) and in 0/336 run-16 prompts. Action agreement 332/336.

## What the lane did

| Step (UTC) | Action | Mark | Read |
|---|---|---|---|
| 09-13 00:00 | **close** | $2,525 | First step: seeded position −4.6% underwater, below SMA20/EMA12 → cut the loss (realized −$52). Baseline carried it the whole window. |
| 09-17 05:00 | open | $2,441 | Price above SMA/EMA, RSI 62. |
| 09-17 11:00 | **close** | $2,433 | −0.3% and below averages → exit. This one is churn: re-entered next day $43 higher. |
| 09-18 04:00 | open | $2,476 | Held to window end at +8.6%. |

## Reading

- **The lane produces exits.** Zero closes in 336 steps without it; two with it, both justified in the rationale by the block's own fields (P&L vs entry, price vs averages). This is the behavior #34 asked for: the decider now answers the exit question instead of defaulting to hold.
- **The critic engaged**: 11 touched intents vs 0. Nothing bypassed RiskGuard or the desk.
- **Cost of the behavior is churn**: one of the two closes was a −0.3% exit followed by a higher re-entry. The block's "beyond your stated tolerance" wording is doing work without a stated tolerance; a minimum-loss or minimum-age threshold in the block (not in RiskGuard) would likely remove that class of exit.
- **Crude PnL is not the point** (one seeded loss realized early vs carried), but note it is not worse: +$33 vs +$16 on the mark-to-bars model.
- `reduce` (partial close) was never used; the decider went full close both times.

## Verdict

**Stage 1 passes on behavior**: the lane does what it is for, with no errors and the safety chain unchanged. Recommend adding a stated tolerance line to the block before stage 2 (e.g. "a loss smaller than 1% or a position younger than 24 h is not by itself a reason to close") and re-running this exact pair once; that is a prompt-text change inside the gated lane, cheap to replay.

Stage 2/3 are the operator's. Like briefs, the lane has no per-feature shadow: enabling it means live closes on the real paper book (ETH +2%, SPY −0.3% as of 2026-09-29), or a global `TRADER_SHADOW_MODE=true` window with no orders at all. **One gate at a time**: not on the same day as the ETF flip (standing order: on/after 2026-10-03).

Runs: `trader eval show 16` / `trader eval show 17`.
