# Briefs replay gate — stage 1 of the decide-change gate for #1

**Date:** 2026-09-29 · **Symbol:** ETH/USD · **Window:** 2026-09-13 00:00 → 2026-09-27 00:00 UTC (336 hourly steps)
**Config held constant:** desk on, Macro View lane on, historical lanes pinned to each step's timestamp (replay picks the brief/view current at the step, never today's).
**Variable:** brief lane in the decide prompt (`--no-briefs` vs `--briefs`).

| Run | Label | Steps | Errors | Actions | Critic (veto / shrink / approve) | Unrealized (mark-to-bars) |
|---|---|---|---|---|---|---|
| 14 | briefs-off-v7 | 335 | 1 (Nemotron disconnect, transient) | 334 hold, 1 open (09-14 03:00) | 3 / 1 / 0 | +$35.68 |
| 15 | briefs-on-v7 | 336 | 0 | 335 hold, 1 open (09-18 02:00) | 6 / 0 / 1 | +$95.00 |

Sanity: every run-15 prompt carried `[via brief vN]` (336/336); no run-14 prompt did (0/336). Both carried the Macro View.

## Reading

- **Action agreement 333 of 335 steps.** The two runs differ only in *when* the single open lands (09-14 vs 09-18). Both open once on a "price above SMA20/EMA12, RSI > 50" technical read; neither closes anything.
- **The critic touched more intents with briefs on** (7 vs 4): the decider drafted a few more opens that the cross-model critic vetoed. The brief lane nudges the draft rate up slightly; the desk absorbs it.
- **The PnL difference is one trade's timing**, not a signal. The gate runbook says the crude PnL ranks configurations and does not predict live PnL; with one open per run it does not even rank.
- **No kept exits to compare** (zero closes in either run), which is the same exit weakness #34 is about.

## Verdict

Briefs in the decide lane are **behavior-neutral on this window**: no fewer bad opens, no kept exits, no error change. The lane is safe (it drops entries whole under its own budget and never reaches RiskGuard) but the replay gives **no evidence it earns its place** yet. The brief content in the sample is thin (one note plus sentiment restated), which matches the Macro View the decider already sees.

Options for the operator (stage 2/3 of the gate are theirs):
1. **Keep the lane dark** (recommended for now): revisit when briefs carry symbol-specific research the Macro View does not, or after #34 gives the decider an exit review to use brief context on.
2. **Shadow window anyway**: flip `TRADER_BRIEFS_ENABLED=true` with the ETFs still in shadow; cost is nil (briefs already compile); watch the draft rate and critic vetoes for a week.

Runs: `trader eval show 14` / `trader eval show 15`.
