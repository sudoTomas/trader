# Bot #2 replay gate — stage 1 of the decide-change gate for #127

**Date:** 2026-10-22/23 · **Bot:** `bot-2-largecap` (bot id 6, v1) · **Symbols:** AAPL, MSFT, NVDA, AMZN, GOOGL, META
**Window:** 2026-09-29 00:00 → 2026-10-19 20:00 UTC (500 hourly steps; 90 NYSE-hour decisions per symbol per run, 15 sessions). It ends where the shadow head-to-head on #127 begins (2026-10-19 21:27 UTC), so the two legs do not overlap.
**Held constant:** Bot #2 v1's Sources (sentiment, brief, held_position, macro_view), pinned to each step; a $5,000 Sleeve (`Sleeve equity` in the prompt); desk off (Bot #2 has none); decide model nemotron-3-super; the crude one-position book with no seed.
**Variable:** the decide rules: `default` (Bot #1's prompt, byte for byte) vs `equity-largecap` (Bot #2's).
**Samples:** 2 full passes (s1, s2) → 24 runs, 2,160 decisions, run one at a time (one Nemotron call in flight).

**Sources were thin.** The brief and sentiment Sources were **empty for the whole window**: the production store has no sentiment row and no brief for any of the six symbols, ever (the sentiment poller polls held symbols only, and `poll_unheld` is off for Bot #2). 0/2,160 steps recorded a sentiment or brief version. Each prompt therefore saw the 1h technicals, the Macro View (2,016/2,160 steps, 11 distinct versions) and, while the sim held a position, the held-position block (525 steps). The comparison is of the two rule sets on technicals plus the Macro View, not of Bot #2's research lanes.

**Not identical to Bot #1 live.** Bot #1 v3 also reads FRED VIXCLS and runs the desk; the baseline here drops both so only the prompt differs. Replay has no RiskGuard, so requested notionals are unclamped (Bot #2 asked for $200–$2,000).

## Runs

`trader eval show <id>`. Labels are `<prompt>-<symbol>-s<sample>`. Errors: 0 in all 24 runs.

| Symbol | `default` s1 / s2 | actions s1 · s2 | `equity-largecap` s1 / s2 | actions s1 · s2 |
|---|---|---|---|---|
| AAPL | 20 / 42 | 2 open 2 close · 2 open 2 close | 21 / 43 | 90 hold · 2 open 1 close |
| MSFT | 22 / 44 | 90 hold · 90 hold | 23 / 45 | 2 open 2 close · 2 open 2 close |
| NVDA | 24 / 46 | 6 open 5 close · 5 open 5 close | 31 / 47 | 1 open 1 close · 2 open 2 close |
| AMZN | 36 / 48 | 1 open · 1 open 1 close | 37 / 49 | 1 open 1 close · 90 hold |
| GOOGL | 38 / 50 | 1 open · 1 open (short) 1 close | 39 / 51 | 90 hold · 90 hold |
| META | 40 / 52 | 2 open 2 close · 2 open 2 close | 41 / 53 | 90 hold · 90 hold |

Run-to-run stability (same prompt, same symbol, s1 vs s2): 83–90 of 90 steps agree for each pair. Across prompts (same sample) 77–89 of 90 agree; NVDA disagrees most (77–78).

## Scoring

Each replayed `open` is scored from the **open of the next 1h bar** (the bar starting at the decision step), in the position's direction. The horizons are 1 session (6 regular-session bars), 5 sessions (30 bars) and to the latest bar (2026-10-22 19:00 UTC). Drawdown is the worst low (a short's: the worst high) against that entry within the 5-session horizon. Opens in the window's last sessions have no full 5-session forward, so the 5-day columns count fewer opens. "Every step" scores a long opened at every decision step: the unconditional baseline for the symbol and window. Crude PnL is the harness's (mark-to-bars, no costs, per-run book); it ranks configurations, it does not predict live PnL.

### Per symbol (both samples pooled)

| Symbol | Prompt | Decisions | Opens | Fwd 1d | Fwd 5d | Max DD 5d | Fwd to 10-22 | Opens < 0 at 5d | Crude PnL |
|---|---|---|---|---|---|---|---|---|---|
| AAPL | default | 180 | 4 | +0.30% | +0.24% | −1.87% | +4.91% | 0/4 | −$16.43 |
| AAPL | largecap | 180 | 2 | −1.52% | +0.21% | −2.08% | +3.78% | 1/2 | −$15.42 |
| AAPL | every step | 90 | — | +0.13% | +1.16% | −1.16% | | | window +3.04% |
| MSFT | default | 180 | 0 | — | — | — | — | — | $0.00 |
| MSFT | largecap | 180 | 4 | −0.32% | +0.05% | −2.20% | −1.49% | 2/4 | −$32.57 |
| MSFT | every step | 90 | — | +0.20% | +0.96% | −1.82% | | | window +5.91% |
| NVDA | default | 180 | 11 | −0.31% | −2.79% | −4.61% | −6.33% | 9/11 | −$44.72 |
| NVDA | largecap | 180 | 3 | +0.31% | −0.13% | −2.38% | −9.29% | 2/3 | −$72.69 |
| NVDA | every step | 90 | — | −0.36% | −2.49% | −4.30% | | | window −5.56% |
| AMZN | default | 180 | 2 | +0.50% | +3.06% | −0.93% | +0.53% | 0/2 | +$29.61 |
| AMZN | largecap | 180 | 1 | +0.14% | +3.62% | −0.56% | −0.38% | 0/1 | −$9.58 |
| AMZN | every step | 90 | — | +0.39% | +1.26% | −1.98% | | | window +6.97% |
| GOOGL | default | 180 | 2 | +1.56% | +2.72% | −0.88% | +0.43% | 0/1 | −$0.94 |
| GOOGL | largecap | 180 | 0 | — | — | — | — | — | $0.00 |
| GOOGL | every step | 90 | — | −0.01% | −0.66% | −2.59% | | | window +1.31% |
| META | default | 180 | 4 | −0.88% | +4.24% | −1.67% | −9.58% | 0/2 | −$14.62 |
| META | largecap | 180 | 0 | — | — | — | — | — | $0.00 |
| META | every step | 90 | — | −0.28% | −1.97% | −4.35% | | | window −2.21% |

### Total (six symbols, two samples)

| Prompt | Decisions | Errors | Opens | Fwd 1d | Fwd 5d | Max DD 5d | Fwd to 10-22 | Opens < 0 at 1d | Opens < 0 at 5d | Opens < 0 to 10-22 | Crude PnL (s1 / s2) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `default` (Bot #1) | 1,080 | 0 | 23 | −0.07% | −0.62% | −3.21% | −3.76% | 14/23 | 9/20 | 17/23 | −$47.11 (+$17.36 / −$64.47) |
| `equity-largecap` (Bot #2) | 1,080 | 0 | 10 | −0.33% | +0.38% | −2.07% | −2.66% | 4/10 | 5/10 | 6/10 | −$130.26 (−$49.46 / −$80.81) |
| every step, long | 540 | | | +0.01% | −0.29% | −2.70% | | | | | |

## Reading

- **The failure mode the prompt was written against is gone.** Read from each open's own prompt, 21 of `default`'s 23 opens were dip buys: price below both sma_20 and ema_12, the rationale citing "RSI … oversold" and "mean reversion". All 10 of `equity-largecap`'s opens were above both averages. NVDA, the tape the shadow evidence on #127 flagged, makes the point: 11 opens under `default` (9 down at 5 days, −2.79% average) against 3 under `equity-largecap`.
- **Fewer opens, and better-chosen ones over 5 days.** 10 vs 23 opens. At 5 days the candidate's opens average +0.38% against −0.62% for the baseline and −0.29% for an open at every step. Its average 5-day drawdown is −2.07% against −3.21% (unconditional −2.70%). To 2026-10-22 the gap is −2.66% vs −3.76%.
- **But it does not win everywhere.** Outside NVDA the baseline's opens did better at 5 days (+2.03%, n=9, against +0.61%, n=7). `default` bought dips in AAPL, AMZN, GOOGL and META that bounced, in a window where four of the six names rose. Its 1-day forward is also better (−0.07% vs −0.33%). Buying a confirmed uptrend tends to pay a little short-term mean reversion.
- **The crude PnL ranks the baseline higher** (−$47 vs −$130 over two samples, on a $5,000 Sleeve). The reason is the exits. With the held-position block present, `equity-largecap` closes as soon as price falls back below its averages ("violating the uptrend condition for holding a long"), so **every one of its 9 closed trades realized a loss** (median hold 48 h). The baseline's 20 closes also all lost (median hold 22 h). Its crude total is rescued by three positions it never closed (AMZN +$34.50, NVDA +$18.07, GOOGL +$4.57 in s1).
- **Small sample.** 10 opens, two passes, one 15-session window. The two passes disagree on whether the candidate opens AAPL at all (0 vs 2) and AMZN (1 vs 0). Nothing here is statistically separable; it ranks behavior.

## Verdict

**Bot #2's replay clears the gate on behavior, narrowly, and not on crude PnL.** The gate asks first whether the candidate is a different animal in its action mix, has errors, and makes fewer bad opens; the crude PnL comes last. On those terms it passes:

- no errors;
- under half the opens;
- none of them dip buys;
- fewer losers at 1 day (4/10 vs 14/23) and to date (6/10 vs 17/23);
- better 5-day forward returns and shallower drawdowns than both the baseline and the unconditional entry;
- the documented NVDA failure removed.

It does not dominate. On the four names that rose, the baseline's dip buys did better, and the candidate's crude book lost more, because its trend-break exits whipsaw (9/9 closed trades negative).

For #127 on 2026-10-27: this leg supports moving Bot #2 forward if the shadow score agrees. A live flip is still a separate gate step with its own risk. Watch the exits, not the opens. If the shadow window shows the same open-then-cut pattern, the remedy is a non-numeric holding clause in the `equity-largecap` rules or a RiskGuard-side stop, never a numeric threshold in the prompt (see the exit-review report's run 18). Also note that the prompt's sentiment and brief rules were never exercised here. With `poll_unheld` off they will stay unexercised live until Bot #2 holds something.

## Reproduce

Code: `config={"prompt_variant": ..., "sleeve_usd": ...}` on a replay run (`src/trader/eval/replay.py`), driven by `scripts/replay_bot_compare.py`:

```bash
uv run python scripts/replay_bot_compare.py run --out runs-s1.json --sample 1   # 12 runs, sequential
uv run python scripts/replay_bot_compare.py score --runs runs-s1.json --out scores-s1.json
```

Run with the xAI credentials blanked (`TRADER_XAI_TOKEN_KEY= XAI_API_KEY=`): decide is Nemotron, and a second process must never rotate the live OAuth refresh token. These runs took about 12 s per step next to the live loop's decides.
