# Bot #2 replay: adding the OpenBB and EDGAR Sources (#155)

Date: 2026-11-18. Window: 2026-11-05 00:00 UTC to 2026-11-18 21:00 UTC, 1h bars, 60 decided
steps per run (market hours only). Replayed as `bot-2-largecap` (`trader eval replay --bot
bot-2-largecap`, decide `grok:grok-4.3`, sleeve $5,000, `equity-largecap` prompt, #139 cost
model). Two arms, two samples each, six stocks: 24 runs (ids 299-322; 298 is a duplicate of
299 from a restarted runner and is ignored).

- **base:** Bot #2's current Sources.
- **new:** the same plus `earnings`, `options`, `fundamentals`, `sec_filings`
  (`--add-source`).

**Verdict: do not subscribe.** The new arm lost more in both samples, and the gap is larger
than the sample-to-sample spread of either arm.

## Net P&L (USD, after costs)

| Symbol | base s1 | base s2 | new s1 | new s2 |
|---|---|---|---|---|
| AAPL | 0.00 | 0.00 | 0.00 | 0.00 |
| MSFT | -66.25 | -137.12 | -94.85 | -171.12 |
| NVDA | -420.39 | -441.50 | -615.44 | -543.40 |
| AMZN | 70.05 | 20.30 | 8.41 | 87.40 |
| GOOGL | -4.30 | 63.22 | 71.57 | 4.08 |
| META | 82.66 | 75.92 | 38.21 | 76.31 |
| **Total** | **-338.23** | **-419.18** | **-592.10** | **-546.73** |

- Arm means: base -378.71, new -569.42, a difference of -190.71.
- Sample-to-sample spread: base 80.95, new 45.37.
- Paired by sample (new minus base): s1 -253.87, s2 -127.55. Both negative.

Most of the gap is NVDA (-297 of the combined -381). Without NVDA the paired differences are
-58.7 and -25.6: still negative, but inside the noise. So the honest reading is "no evidence
the new Sources help, and NVDA got worse", not "the new Sources hurt everywhere".

## Opens and hit rate

Hit rate is closed round trips with a positive price move (open price to close price, long).

| Arm | opens | round trips | wins | hit rate |
|---|---|---|---|---|
| base | 31 | 27 | 6 | 22% |
| new | 29 | 24 | 7 | 29% |

The arms trade at about the same frequency. The slightly higher hit rate in the new arm
does not offset its larger losers.

## What the new arm saw

Every new-arm run journaled `earnings`, `fundamentals` and `options` versions on its
decisions, so the Sources reached the prompt. `sec_filings` versions appear only in the AAPL
runs. AAPL held on every step in both arms.

## Caveats

- One window, a falling tape for NVDA and MSFT. Bot #2 lost money in both arms.
- Mark-to-bars P&L with one position; it ranks configurations, not live P&L.
- Cost: $3.35 of Grok for the 24 runs (base $0.12, new $0.16 per run on average).

## Decision

Bot #2 keeps its current Sources. Per `docs/runbooks/decide-change-gate.md`, a second replay
is worth running once the snapshots cover a window that is not dominated by one symbol's
drawdown.
