# Blind-replay baseline: named vs blind vs control (#140)

**Date:** 2026-11-03/04 · **Bots:** `bot-1` (v3) and `bot-2-largecap` (v2) · **Symbols:** Bot #1's ten equity/ETF symbols, Bot #2's six large caps (ETH/USD skipped)
**Window:** 2026-10-24 00:00 → 2026-11-03 00:00 UTC, 36 market-hour decisions per run. Runs 94–141 (`trader eval show <id>`), 48 runs, 0 errors.
**Models:** the `.env` chains, not the live Profile: decide and analyst on the local nemotron-3-super, critic on `grok-4`. One sample per arm.
**Costs:** every fill pays the #139 cost model (5 bp half-spread, 2 bp fee, sqrt impact).

**Arms.**
- `named`: the prompt as live, with ticker, dates, prices in dollars, the Macro View and briefs.
- `named-control`: the same, with the Sources blinding drops (Macro View, briefs) removed. Ticker and dates stay.
- `blind`: ticker as `ASSET_A`, time as bar t-k, prices indexed to 100, sizes as % of the Sleeve, the same Sources dropped.

So `named − control` is what the dropped Sources were worth, and `control − blind` is what the model's memory of the name and dates was worth.

## Reading

- **Every arm lost money, before and after costs.** Bot #1 gross P&L: named −$859, control −$1,209, blind −$1,472. Costs added $80, $130 and $318. No arm shows an edge to protect or to blame on memory.
- **Bot #1, Sources:** `named − control` = **+$400**. The research mostly helped by keeping the bot out: with the Macro View and briefs it never opened GLD, SLV or USO; without them it opened and lost on all three.
- **Bot #1, memory:** `control − blind` = **+$452**, but it is spread thin and switches sign by symbol. Blind won on AMZN and MSFT, and lost $956 on GOOGL alone, where control lost $219. Take GOOGL out and the memory gap is about −$285, i.e. noise.
- **Blind trades more and pays for it.** Blind opened 28 times against 19 named and paid four times the costs. The hit rate is higher (0.12 vs 0.06) on small counts; both are poor.
- **Bot #2 barely trades on replay** (3 opens named, 1 control, 0 blind in 108 decisions per arm). There is nothing to separate here; its gate is the live AAPL shadow on #127.

**Verdict.** No sign that the decide model trades on memorised outcomes for these names: removing the names did not improve results, and the one large gap is a single symbol. The research Sources earn their place as a brake on poor entries, not as a source of winners. The edge problem is the decision itself, which is what #141–#144 (more inputs, lessons, new signals) are for. One week and one sample per arm is thin; replay is not deterministic (see `2026-10-22-bot-2-replay-gate.md`), so read the per-symbol gaps as noise until a second sample agrees.

## Tables

| Bot | Symbol | Net named | Net blind | Net gap | Hit named | Hit blind | Opens named | Opens blind |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| bot-1 | AAPL | -102.07 | -95.09 | -6.97 | 0.00 | 0.00 | 2 | 2 |
| bot-1 | AMZN | -187.49 | 69.23 | -256.72 | 0.00 | 0.50 | 4 | 2 |
| bot-1 | GLD | 0.00 | -112.35 | 112.35 | - | 0.00 | 0 | 4 |
| bot-1 | GOOGL | -172.63 | -956.41 | 783.78 | 0.00 | 0.00 | 5 | 6 |
| bot-1 | META | -184.72 | -69.70 | -115.02 | 0.00 | 0.00 | 3 | 2 |
| bot-1 | MSFT | -286.57 | 160.87 | -447.44 | 0.00 | 0.50 | 2 | 2 |
| bot-1 | NVDA | 4.93 | -159.68 | 164.61 | 0.50 | 0.00 | 2 | 2 |
| bot-1 | SLV | 0.00 | -160.22 | 160.22 | - | 0.00 | 0 | 3 |
| bot-1 | SPY | -10.59 | -99.32 | 88.73 | 0.00 | 0.00 | 1 | 2 |
| bot-1 | USO | 0.00 | -367.90 | 367.90 | - | 0.33 | 0 | 3 |
| bot-2-largecap | AAPL | -37.57 | 0.00 | -37.57 | 0.00 | - | 1 | 0 |
| bot-2-largecap | AMZN | -7.19 | 0.00 | -7.19 | 0.00 | - | 1 | 0 |
| bot-2-largecap | GOOGL | 0.00 | 0.00 | 0.00 | - | - | 0 | 0 |
| bot-2-largecap | META | 0.00 | 0.00 | 0.00 | - | - | 0 | 0 |
| bot-2-largecap | MSFT | 0.00 | 0.00 | 0.00 | - | - | 0 | 0 |
| bot-2-largecap | NVDA | -221.70 | 0.00 | -221.70 | 0.00 | - | 1 | 0 |

| Bot | Arm | Net P&L | Hit rate | Opens | Trades |
|---|---|---:|---:|---:|---:|
| bot-1 | named | -939.14 | 0.06 | 19 | 17 |
| bot-1 | blind | -1,790.57 | 0.12 | 28 | 26 |
| bot-1 | named-control | -1,338.73 | 0.09 | 23 | 22 |
| bot-2-largecap | named | -266.46 | 0.00 | 3 | 3 |
| bot-2-largecap | blind | 0.00 | - | 0 | 0 |
| bot-2-largecap | named-control | -100.40 | 0.00 | 1 | 1 |
