# Issue tracker

Issues live on **GitHub**: [sudoTomas/trader/issues](https://github.com/sudoTomas/trader/issues).

This repo used Linear (team Eastwood AI, prefix **EAI-**, project trader) through M4.
On 2026-09-19 the remaining open work was reconstructed from `HANDOFF.md` and filed
here. This client could not query or close Linear — cancel the Linear **trader**
project in the Eastwood AI workspace so agents stop targeting it.

Historical `EAI-*` strings in tests and comments are provenance, not the tracker.
`v1:EAI-*` cites the frozen `trading-agent` archive. embry0 stays on GitHub
(`sudoTomas/embry0`).

## Open (filed 2026-09-19)

| GitHub | Was | What |
|---|---|---|
| [#1](https://github.com/sudoTomas/trader/issues/1) | EAI-192 | Flip briefs into the decide lane |
| [#2](https://github.com/sudoTomas/trader/issues/2) **closed 2026-09-19** | EAI-176 / epic EAI-164 | First ETH round trip accepted (journal ↔ Alpaca match; flatten skipped) |
| [#3](https://github.com/sudoTomas/trader/issues/3) | EAI-186 leftover | SPY equity round trip (still held) |
| [#4](https://github.com/sudoTomas/trader/issues/4) | EAI-187 | Commodity ETFs |
| [#5](https://github.com/sudoTomas/trader/issues/5) | EAI-189 | Screener |
| [#6](https://github.com/sudoTomas/trader/issues/6) | EAI-191 | IBKR decision record (deferred) |
| [#7](https://github.com/sudoTomas/trader/issues/7) | EAI-173 | Probe SuperGrok OAuth vs `/v1/responses` |
| [#8](https://github.com/sudoTomas/trader/issues/8) | — | Capture-scoped API key |
| [#9](https://github.com/sudoTomas/trader/issues/9) | — | crypto-flows Grok desk seat |
| [#10](https://github.com/sudoTomas/trader/issues/10) | — | Sweep citation loop |
| [#11](https://github.com/sudoTomas/trader/issues/11) | — | Wire `ApprovalService.redeem()` |
| [#12](https://github.com/sudoTomas/trader/issues/12) | — | Count `trades_today` |
| [#13](https://github.com/sudoTomas/trader/issues/13) | — | Alpaca account equity, not `shadow_equity` |
| [#14](https://github.com/sudoTomas/trader/issues/14) | — | Persist `decision_id` on orders |
| [#15](https://github.com/sudoTomas/trader/issues/15) | — | `Mark.realized_vol` / `unrealized_pnl_frac` |
| [#16](https://github.com/sudoTomas/trader/issues/16) **closed** | — | Usage query (`trader usage` / `GET /usage` / `get_usage`) |
| [#17](https://github.com/sudoTomas/trader/issues/17) | — | Call FRED |
| [#18](https://github.com/sudoTomas/trader/issues/18) | — | Replay includes Macro View and briefs |
| [#19](https://github.com/sudoTomas/trader/issues/19) | EAI-166 | **Epic:** M5 expansion |
| [#20](https://github.com/sudoTomas/trader/issues/20) | — | **Epic:** box-local Grok macro desk |
| [#21](https://github.com/sudoTomas/trader/issues/21) | — | **Epic:** money-path honesty |

Milestone: [M5 Expansion](https://github.com/sudoTomas/trader/milestone/1).

## Done in Linear (not re-filed)

M0–M4 plus the money-path/desk work that earned its flip: EAI-161…180 (except 173/176
leftovers above), EAI-186 slice (SPY in universe), EAI-188 replay harness, EAI-190
desk, EAI-193 Robinhood no-go, EAI-194 LAN auth, EAI-195 operator doors (approve/adopt
surfaces exist; redeem is #11).
