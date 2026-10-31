# Sentiment churn: a minimum hold for fresh positions

**Date:** 2026-10-30 · **Bot:** #1 (`bot-1`), ETFs SPY/GLD/SLV/USO, live paper ·
**Code:** main `9a7692d`; the fix is `c0b9411` on branch `worktree-agent-a75e394a0bc734eff`.
**Production state at the time of writing:** the spark's checkout is still at `4df849d`
(#133) and `trader-api` has run since 2026-10-20, so PR #136 (the IEX fix, the untilted
wording and the volatility stop) is merged but **not yet deployed** there. Every live
row below was made by the pre-#136 code.

## Answer

On 2026-10-26 Bot #1 bought SPY at 15:33 UTC (766.68), sold it at 16:04 (765.18) and
bought it back at 16:35 (765.28). That is not a one-off. Four of Bot #1's fifteen
executed model closes came within 1.5 hours of the open they closed, and three of those
four were bought back about 30 minutes later within 0.01% of the exit price. Each round
trip paid the spread twice and gained nothing.

The fix is a deterministic **minimum hold** in RiskGuard: the model's close of a position
its Bot opened less than 4 hours ago is denied (`TRADER_RISK_MIN_HOLD_SECONDS`, default
14400; `0` turns it off). Only the model's Draft is held. The deterministic stop is never
held, and no other caller is either, so the stop is what protects a fresh position. The
two prompt options did not work: the 10/26 close survives both of them 7 times out of 7,
and showing the sentiment's usual range made the model quote the number as proof.

In the replay against main (24 walk runs, desk off, the #136 stop in both), the hold
removed the one fast close and the one round trip main made, never got in the way of a
stop, and came out slightly ahead on crude P&L and on drawdown (−$3 against −$18 total; sum
of worst points −$208 against −$224). Replay reproduces little of the live churn (hourly
steps and no desk), so the case for the rule rests on the live record and the re-decides;
the replay shows what it costs, which is little.

## How often it happens

Bot #1's executed model closes, from `orders` joined to `decisions`, with the age measured
from the Bot's last executed open on the symbol. "+1h" and "+4h" are the close of the
1st and 4th regular-session 1h SIP bar (or hourly crypto bar) after the exit fill.

| Order | Symbol | Close (UTC) | Age | X sentiment in the block | P&L shown | +1h | +4h | Bought back |
|---|---|---|---|---|---|---|---|---|
| 10 | GLD | 10-14 16:34 | **1.5 h** | −0.20 (normal) | −0.29% | +0.07% | **+1.78%** | **30 min later, +0.01%** |
| 17 | GLD | 10-19 14:02 | **0.5 h** | +0.35 (quiet) | −0.36% | −0.20% | −0.10% | 24.5 h later |
| 19 | USO | 10-19 14:49 | **0.3 h** | −0.35 (normal) | −1.92% (phantom) | +0.45% | +0.50% | **30 min later, −0.01%** |
| 29 | SPY | 10-26 16:04 | **0.5 h** | −0.25 (normal) | −0.18% | +0.10% | +0.14% | **31 min later, +0.01%** |
| 13 | USO | 10-16 14:45 | 23 h | +0.65 (elevated) | −0.29% | +1.33% | +3.61% | 72 h later |
| 23 | USO | 10-20 14:02 | 23 h | −0.20 (normal) | +0.59% | −0.82% | −0.38% | no |
| 25 | GLD | 10-21 19:04 | 28 h | −0.35 (quiet) | −1.44% | −1.32% | −1.81% | no |
| 14 | GLD | 10-16 16:03 | 47 h | +0.30 (normal) | +0.42% | −0.02% | −0.25% | 70 h later |
| 26 | SLV | 10-22 13:32 | 48 h | −0.25 (normal) | −3.36% | −0.43% | −1.07% | no |
| 27 | SPY | 10-22 15:02 | 49 h | +0.15 (normal) | −0.65% | +0.10% | +0.13% | 97 h later |
| 31 | SPY | 10-30 14:32 | 94 h | −0.35 (normal) | −0.94% | −0.02% | −0.81% | no |

Not shown are four closes whose open has no journaled decision (ETH 9/18, the two ETH
closes of 10/02, and SPY 10/16, order 15); each of those positions was days old. In total,
4 of Bot #1's 15 executed model closes came within 2 hours of their open, and none of the
rest within 22 hours. Price rose within four hours after 3 of those 4,
and 3 of the 4 were bought back at the same price half an hour later.

**Sentiment is cited in 13 of the 15 close rationales.** The 10/26 one rests on it
alone: "The original thesis of mildly positive sentiment (0.15) is contradicted by the latest
X sentiment of −0.25 (normal)". The open it reversed, 31 minutes earlier, cited the same
+0.15 as support. The research analyst on the desk (live on Bot #1) amplified it in the
same prompt: "the sentiment divergence weakens the original thesis". (The technical analyst
in that prompt also said price 765.32 was "below EMA12 (763.06)", which is false. That is a
separate desk defect.)

**A move like 0.15 → −0.25 is ordinary for these polls.** Over every scored poll in
`sentiment_scores`:

| Symbol | Polls | Mean | SD | Median poll-to-poll move | 90th pct move | Moves ≥ 0.4 | Sign flips |
|---|---|---|---|---|---|---|---|
| SPY | 184 | −0.06 | 0.34 | 0.15 | 0.50 | 24% | 25% |
| ETH/USD | 746 | +0.02 | 0.38 | 0.25 | 0.60 | 36% | 36% |
| GLD / SLV / USO | 6–11 each | | 0.23–0.39 | 0.10–0.25 | 0.48–0.76 | ~40% | |

**And it predicts nothing.** Correlating each poll-to-poll change with the price move that
followed: SPY r = −0.04 at 1 hour and +0.02 at 4 hours (183 pairs); ETH r = +0.02 and −0.02
(745 pairs). After a drop of 0.3 or more at normal volume, SPY was up four hours later 8
times in 22 (mean −0.13%, against −0.04% for every poll), and ETH 64 times in 111 (mean
+0.00%). After a *rise* of 0.3 or more SPY did no better (9 up in 32, mean −0.14%). The
scores are noise at the horizon the exit lane works on.

## The options

### 1. Wording: tell the exit review a normal-volume sentiment change is not evidence

Tested with the journal re-decide of the earlier report (`exit_redecide.py`), adapted as
`~/tmp/trader-churn-scripts/redecide_churn.py` on the spark: the fourteen journaled
close prompts, each rebuilt with main's untilted ask, asked again at temperature 0.2,
serially, on the live Nemotron. Everything else in the prompt (bars, brief, analyst notes)
is the journal's. Variant B adds one system line: "X sentiment is a noisy hourly poll. A
change in its score at normal or quiet volume is not, by itself, evidence that the thesis
has broken."

### 2. Data: show the change and its usual range

Variant C extends the sentiment line with the reading at entry and the symbol's own range,
computed from `sentiment_scores` before the decision, e.g. for 10/26: "Latest X sentiment:
−0.25 (normal) …; at entry +0.15 (change −0.40); SPY's score usually moves 0.20 between
polls, and 1 poll in 10 moves more than 0.50".

| Re-decide | 14 close prompts × 2 | 10/26 SPY (8631) | 10/30 SPY (9867) |
|---|---|---|---|
| main (untilted wording, #136) | 6 closes of 28 | 5 of 7 close | 7 of 7 close |
| B: "not by itself evidence" | 5 of 28 | **7 of 7 close** | 7 of 7 close |
| C: change and usual range shown | **8 of 28** | **7 of 7 close** | 7 of 7 close |

(The 8631 and 9867 columns are the 2 reps of the full run plus 5 targeted reps.) Neither
option touches the churn. B moves one unrelated SPY close to a hold and keeps 10/26. C is
worse than doing nothing: it adds closes (USO 10/19, ETH 10/02) and the model reads the
range backwards, as in "latest X sentiment is −0.25, a −0.40 shift from entry, exceeding
typical moves (0.20 between polls)". The number exceeds the *median* move, so the model
treats it as significant. This is the third time a number placed in the exit prompt has
been applied mechanically and wrongly (run 18 in `2026-09-29-exit-review-replay-gate.md`,
the "typical daily move" line in `2026-10-22-bot-1-exits.md`, and now this). A wording fix
would also have to reach the desk's analysts, which repeat the sentiment argument inside
the same prompt.

### 3. A deterministic minimum hold (chosen)

This needs no model cooperation. It stops every exit/re-open flip of a fresh position,
whatever the cause (sentiment, a phantom mark, an analyst's misread), and it is cheap to
reason about: during the hold the position is exactly as protected as the stop makes it,
and the stop is unchanged.

**RiskGuard or the lane?** The sell path is runner → (desk, Confirm) → `RiskGuard.evaluate`
→ executor; the stop takes the same `evaluate` from `runner._stop`. I put the rule in
RiskGuard, as a check on exits:

- It is a limit, and limits are code in the one chain. It sits beside its mirror, the stop
  cooldown (no opens for 24 h after a stop; now also no model closes for 4 h after an open),
  and reads the journal the same way.
- A deny there is journaled and audited like every other verdict, so a held close shows in
  the decision row as the model's `close` Draft, status `denied`, last verdict `min_hold`.
- RiskGuard does not know where an intent came from, so the caller says: `evaluate(...,
  draft=True)` is passed only by the runner's model path. The default is *no hold*, so the
  stop, approval redemption and any future caller that forgets the flag can always exit.
  Failing that way churns; failing the other way could trap a position past its stop.

The lane alternative (turn the Draft into a hold in the runner, as the Confirm will) would
work too, but it puts a limit outside RiskGuard and needs its own journaling.

## The fix (`c0b9411`)

- `RiskLimits.min_hold_seconds` from `TRADER_RISK_MIN_HOLD_SECONDS` (default **14400**,
  `0` = off; validated ≥ 0). It shows in `trader risk status` under `limits`.
- `RiskContext.opened_at`, and a check **4b, min hold**, on the reducing branch after the
  kill switches and class policy: a reducing intent whose `opened_at` is less than the hold
  ago is denied with "opened at …; the model may not close it before … (the stop still
  can)". Pause and drift still deny first.
- `RiskGuard.evaluate(..., draft=False)`: with `draft=True` (and a hold configured) it looks
  up the Bot's last `executed` decision with action `open` on the symbol. A shadow or denied
  open starts no hold, and Bots are kept apart (`coalesce(bot_id, 1)`, as for the stop
  cooldown).
- The runner passes `draft=True` for the model's Draft only.
- Replay runs the same rule in its crude book (`min_hold_denied` in the summary, `min_hold:
  true` on the step) from the sim's own opens and from seeds that carry `opened_at`.
- Tests: `tests/test_min_hold.py` (14): the pure chain both sides of the boundary, off at
  0, opens untouched, kill switch first, the lookup (executed only, per Bot), the loop
  denying a model close of a 31-minute-old position, the stop closing a 5-minute-old one,
  and replay. `uv run pytest -q`: 884 passed. `ruff check` and `ruff format --check` clean.
- Docs: `CONTEXT.md` (Minimum hold), `docs/architecture.md`, the gate runbook,
  regenerated `docs/configuration.md` and `.env.example`.

**Why 4 hours.** The four short closes came at 15, 28, 30 and 92 minutes, and no Bot #1
model close fell between 2 and 22 hours, so anything from 2 to 22 hours stops all four. A
thesis built on 1h bars has had about four new bars to be judged on after 4 hours; the
sentiment poller has polled about four times, which is enough for the score to wander
through its whole range, so a reading then says as little as one does at 30 minutes. Longer
holds only lengthen the time when the stop alone protects. The stop sits 1.3% (SPY) to 5%
(USO) away under the #136 settings, so the most a hold can cost is bounded by the stop.

It also applies to Bot #2 (AAPL live). Bot #2 has never placed an order, so there is no
history to check.

## Replay against main

Twelve walk replays per side with the harness the loop ships (`ReplayHarness`), exit
review on, Macro View and briefs on, desk off, the live Nemotron, one call at a time
(Bot #2 was live on the same model throughout). Both sides run the #136 stop from the live
settings (8% ceiling, 2× vol). **main** is a copy of `9a7692d` (runs 70, 72, … 92);
**candidate** is a copy of `c0b9411`, which adds the 4 h hold (runs 71, 73, … 93). Run
pairs alternate, main then candidate. Scripts on the spark:
`~/tmp/trader-churn-scripts/walk_churn.py` and `score_churn.py`. Opens are clamped to
the live $1,000; P&L is crude mark-to-bars with no costs, so it ranks the two sides and
predicts nothing.

- **Flat** (4 runs a side): SPY, GLD, SLV and USO from 10/14 13:00 to 10/31, starting
  flat, so the model opens and the exit lane reviews its own fresh positions. This is where
  churn can happen.
- **Seeded** (8 a side): the eight live positions of `walk_replay2.py` in the 10/22
  report, each seeded with its live qty, entry and `opened_at`. These check the stop.

| | main flat | candidate flat | main seeded | candidate seeded |
|---|---|---|---|---|
| Opens | 7 | 6 | 0 | 0 |
| Model closes | 3 | 3 | 3 | 2 |
| … within 4 h of the open | **1** | **0** | 0 | 0 |
| Closes held by the min hold | — | 1 | — | 0 |
| Round trips (re-open within 4 bars of a close) | **1** | **0** | 0 | 0 |
| Stop closes | 1 | 0 | 1 | 2 |
| Closes followed by a rise 4 bars later | 1 of 4 | 2 of 3 | 0 of 3 | 0 of 3 |
| Crude P&L | +$6.43 | +$21.32 | −$24.34 | −$24.81 |
| Sum of worst running P&L | −$78.70 | −$63.81 | −$145.44 | −$143.92 |

**The one churn the replay produced:** main opened SLV on 10/14 at 14:00, closed it two
bars later at −1.11% (price rose 1.40% over the next four bars) and bought it back. The
candidate held the same close at the 3-hour step, closed at the 4-hour step at −1.41%
(then +2.57%), and did not re-open. So here the hold cost 0.3% on the exit and saved the
round trip. On these ETFs, holding to the end of the window would have done better than
either.

**Everything else is the same run on both sides or sampling noise** (temperature 0.2): the
other flat closes are days old (51–217 h), six of the eight seeded runs are step-for-step
identical, and the two that differ (GLD 10/19 closes at 14:00 instead of 18:00; GLD 10/20 is
stopped on 10/23 instead of being closed by the model a day earlier) differ at ages of 72
hours, far past any hold. The P&L gap between the sides comes mostly from those
divergences, not from the rule.

**The stop still fires.** Two stops fired on the candidate's side (SPY 10/26 seed at
−1.65% on 10/30 18:00, the same step as main; GLD 10/20 seed at −3.17% on 10/23 14:00), and
neither was delayed. Main's two (that SPY seed, and a flat SPY position at 216 h) fired
with no hold to interact with. No position in these windows crossed its stop inside a 4-hour hold, so the replay
does not exercise that case; `test_the_stop_closes_a_fresh_position_inside_the_hold`
(the loop) and the replay test do, and the rule cannot touch the stop's path by
construction: `runner._stop` never passes `draft`.

**What the replay cannot show.** The live churn came from 30-minute heartbeats and from the
desk's analysts repeating the sentiment argument. Replay steps hourly with the desk off, so
it reproduced one fast round trip in eight flat runs where live Bot #1 made three in two
weeks. A desk-on replay (critic on Grok) would cost real money per step and was not run.
The live record is the evidence for the rule; the replay is evidence that it costs little.

## Gate status

- **Stage 1 (replay) is done.** The re-decide rules out the prompt options; the walk
  replay shows the hold removes the fast close and the round trip, leaves the stop alone,
  and does not cost P&L or drawdown in these windows.
- **Ready to merge.** `c0b9411` on `worktree-agent-a75e394a0bc734eff`, on main `9a7692d`.
  `uv run pytest -q`: 884 passed. `ruff check` and `ruff format --check` clean.
- **Deploying is the flip.** The default is on (4 h), and it binds both Bots on the next
  tick. A position opened less than 4 h before the restart is held from its journaled open.
  Rollback is config-only: `TRADER_RISK_MIN_HOLD_SECONDS=0` and a restart.
- **Sequence it after #136.** The spark is still on `4df849d`, so #136 (wide-quote marks,
  untilted wording, volatility stop) is not live yet. That is already one gate. Deploy #136,
  watch a few sessions of `stop_loss` rows and closes, then flip this; don't ship both in
  the same window. If you want to collapse them anyway, the rule is a deterministic limit
  that can only remove model closes, so the overlap is low-risk, but the journal would no
  longer say which change did what.
- **What to watch after the flip:** decisions with status `denied` and last verdict
  `min_hold`, and the closes that follow them at the 4-hour mark. A held close that the stop
  then has to take is the case to look at.
- **Not addressed:** sentiment-driven closes of *older* positions (the 10/30 SPY close at
  94 h closes 7 of 7 under every wording tried). The scores carry no signal at these
  horizons, so whether the sentiment line belongs in the exit block at all is the open
  question. Dropping it is a decide change and a separate gate. The technical analyst's
  misread of price against its EMA in the 10/26 prompt is a separate desk defect.
