# Shadow signals: two weeks of Kronos and funding/OI readings (#154)

Date: 2026-11-18. Window: 2026-11-04 21:09 UTC to 2026-11-18 21:09 UTC (`trader signal-scores
--days 14`; `--bot bot-1` gives the same numbers, since only Bot #1 journals the readings).
2,716 readings, 460 still pending (their 24-bar horizon has not closed). Horizon: 24 1h
bars. No Bot has `in_prompt` on.

**Verdict: neither signal passes. No `in_prompt` proposal.** Kronos's readings look broken
rather than merely weak; fix the sidecar's inputs before judging the model.

## Kronos

Target (runbook section 4): the p10..p90 band covers about 80% of realized 24-bar returns, and
the hit rate is above one half on enough symbols.

| | n | coverage | hit rate |
|---|---|---|---|
| All symbols | 866 | 15.6% | 38.6% |
| Week 1 | 515 | 18.6% | 43.2% |
| Week 2 | 352 | 11.1% | 31.8% |

By symbol, coverage ranges from 0% (NVDA) and 1% (ETH/USD) to 70% (META); the hit rate is
above one half on 4 of 11 symbols (AMZN, GOOGL, META, AAPL at exactly 0.5).

The forecasts themselves are implausible. The average stored reading per symbol:

| Symbol | readings | median 24-bar return | share of paths up | p10..p90 width |
|---|---|---|---|---|
| NVDA | 172 | +19.3% | 1.00 | 16.2% |
| ETH/USD | 1,319 | +9.5% | 0.96 | 8.9% |
| SLV | 132 | +5.7% | 0.90 | 7.5% |
| GLD | 131 | +3.2% | 0.95 | 2.0% |
| GOOGL | 139 | +3.1% | 0.88 | 10.1% |
| SPY | 131 | +2.3% | 0.98 | 1.5% |
| AAPL | 131 | -1.8% | 0.05 | 1.5% |

A model forecasting NVDA up 19% in 24 bars with every sampled path up, or SPY up 2.3% with a
1.5%-wide band, is not miscalibrated; its input or output scaling is wrong. Likely places to
look: how the sidecar normalizes the context window (equity bars have overnight and weekend
gaps), the timestamp features it passes, and how predicted prices are turned into returns
against `last_close`. Until that is fixed these numbers say nothing about Kronos.

### Cause, found the same day

Not a scaling bug. Kronos z-scores its whole input window, and the sidecar sent 400 1h
bars: about 17 days for ETH and about 2.5 months of equity sessions. Kronos-small's
forecast is then roughly "back to the window's mean within 24 bars". After the November
selloff that mean sat far above the last price (ETH +17.5%, NVDA +30.5%), and the median
forecasts matched it (+19.3%, +40.7%). A driftless random walk also came out with every path
up. `predict` and `predict_batch` agree, on GPU and CPU.

Offline check (7 symbols, 20 non-overlapping 24-bar points each, n = 140 per row):

| Context | Coverage of p10..p90 | Hit rate |
|---|---|---|
| 400 bars | 19% | 56% |
| 128 bars | 40% | 55% |
| 64 bars | 36% | 58% |

The lookback is now 128 bars. Coverage is still about half the target (the 32 sampled paths
are too tightly bunched), and a hit rate of 55% on 140 points is within noise. Re-score
after two more weeks on 128 before judging Kronos again.

## Funding rate and open interest (crypto_derivatives)

Target: a rank IC with a stable sign across both weeks. Only ETH/USD has readings; basis
returned nothing (n = 0).

| Factor | 14 days (hourly) | Week 1 | Week 2 | Daily, non-overlapping (n = 14) | Daily week 1 / week 2 (n = 7 each) |
|---|---|---|---|---|---|
| funding_rate | -0.40 (n 305) | -0.35 | -0.61 | -0.37 | 0.00 / -0.79 |
| oi_change_24h | -0.19 (n 305) | -0.19 | -0.15 | -0.53 | -0.43 / -0.54 |

Hourly readings 24 bars ahead overlap, so n = 305 overstates the evidence: one reading a day
leaves 14 independent observations. Both factors are negative in both weeks on the hourly
series, but that rests on one symbol over a single selloff (ETH fell through the window), and
the funding IC is 0.00 on week 1's daily sample. That is not enough to put a factor in front of
a decide model. Keep journaling; re-score when there are more symbols or more weeks, and look
at whether the sign survives a window where ETH rose.

## Next

- Kronos lookback lowered to 128 bars; re-score in two weeks.
- Keep `TRADER_SHADOW_SIGNALS_ENABLED=true`; no `in_prompt` change.
