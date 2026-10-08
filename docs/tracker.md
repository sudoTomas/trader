# Issue tracker

Issues live on **GitHub**: [sudoTomas/trader/issues](https://github.com/sudoTomas/trader/issues).

This repo used Linear (team Eastwood AI, prefix **EAI-**, project trader) through M4.
On 2026-09-19 the remaining open work was reconstructed from `HANDOFF.md` and filed
here. This client could not query or close Linear — cancel the Linear **trader**
project in the Eastwood AI workspace so agents stop targeting it.

Historical `EAI-*` ids in this file and in `docs/research/`, `docs/runbooks/`, and
`docs/hermes/` are provenance, not the tracker; the lessons those tickets taught are
catalogued by slug in [`lessons.md`](lessons.md), which is what code comments cite.
embry0 stays on GitHub (`sudoTomas/embry0`).

## Open (filed 2026-09-19)

| GitHub | Was | What |
|---|---|---|
| [#1](https://github.com/sudoTomas/trader/issues/1) | EAI-192 | Flip briefs into the decide lane |
| [#2](https://github.com/sudoTomas/trader/issues/2) **closed 2026-09-19** | EAI-176 / epic EAI-164 | First ETH round trip accepted (journal ↔ Alpaca match; flatten skipped) |
| [#3](https://github.com/sudoTomas/trader/issues/3) | EAI-186 leftover | SPY equity round trip (still held) |
| [#4](https://github.com/sudoTomas/trader/issues/4) **closed 2026-09-29 (PR #59; shadow-first)** | EAI-187 | Commodity ETFs |
| [#5](https://github.com/sudoTomas/trader/issues/5) **closed 2026-09-29 (PR #60)** | EAI-189 | Screener |
| [#6](https://github.com/sudoTomas/trader/issues/6) **closed 2026-09-29** | EAI-191 | IBKR decision record (deferred; re-file when wanted) |
| [#7](https://github.com/sudoTomas/trader/issues/7) **closed 2026-09-29 (matrix recorded, 530cfb0)** | EAI-173 | Probe SuperGrok OAuth vs `/v1/responses` |
| [#8](https://github.com/sudoTomas/trader/issues/8) **closed 2026-09-29 (PR #58)** | — | Capture-scoped API key |
| [#9](https://github.com/sudoTomas/trader/issues/9) **closed 2026-09-29 (PR #66)** | — | crypto-flows Grok desk seat |
| [#10](https://github.com/sudoTomas/trader/issues/10) **closed 2026-09-29 (PR #66)** | — | Sweep citation loop |
| [#11](https://github.com/sudoTomas/trader/issues/11) **closed 2026-09-29 (PR #39)** | — | Wire `ApprovalService.redeem()` |
| [#12](https://github.com/sudoTomas/trader/issues/12) **closed 2026-09-29 (PR #40)** | — | Count `trades_today` |
| [#13](https://github.com/sudoTomas/trader/issues/13) **closed 2026-09-29 (PR #40)** | — | Alpaca account equity, not `shadow_equity` |
| [#14](https://github.com/sudoTomas/trader/issues/14) **closed 2026-09-29 (PR #40)** | — | Persist `decision_id` on orders |
| [#15](https://github.com/sudoTomas/trader/issues/15) **closed 2026-09-29 (PR #40)** | — | `Mark.realized_vol` / `unrealized_pnl_frac` |
| [#16](https://github.com/sudoTomas/trader/issues/16) **closed** | — | Usage query (`trader usage` / `GET /usage` / `get_usage`) |
| [#17](https://github.com/sudoTomas/trader/issues/17) **closed 2026-09-29 (PR #37)** | — | Call FRED |
| [#18](https://github.com/sudoTomas/trader/issues/18) **closed 2026-09-29 (PR #37)** | — | Replay includes Macro View and briefs |
| [#19](https://github.com/sudoTomas/trader/issues/19) | EAI-166 | **Epic:** M5 expansion |
| [#20](https://github.com/sudoTomas/trader/issues/20) | — | **Epic:** box-local Grok macro desk |
| [#21](https://github.com/sudoTomas/trader/issues/21) **closed 2026-09-29** | — | **Epic:** money-path honesty |

## Filed 2026-09-29 (review of live performance + open-source readiness)

| GitHub | What |
|---|---|
| [#22](https://github.com/sudoTomas/trader/issues/22) **closed 2026-09-29 (PR #40)** | Sentiment: poll every 3h, held symbols only (spend tripled after x_search repricing) |
| [#23](https://github.com/sudoTomas/trader/issues/23) **closed 2026-09-29 (PR #39)** | Clamp unattended opens to the approval threshold instead of parking them |
| [#24](https://github.com/sudoTomas/trader/issues/24) **closed 2026-09-29 (PR #40)** | Breakers: roll `day_session` every tick, not only on intents |
| [#25](https://github.com/sudoTomas/trader/issues/25) **closed 2026-09-29 (PR #36)** | Broker/data errors lose their detail (`alpaca: `) |
| [#26](https://github.com/sudoTomas/trader/issues/26) **closed 2026-09-29 (PR #38)** | Generic decide LLM provider (any OpenAI-compatible endpoint) |
| [#27](https://github.com/sudoTomas/trader/issues/27) **closed 2026-09-29 (PR #54)** | Config reference generated from Settings; complete `.env.example` |
| [#28](https://github.com/sudoTomas/trader/issues/28) **closed 2026-09-29 (PR #36)** | License (MIT), disclaimer, contribution scaffolding |
| [#29](https://github.com/sudoTomas/trader/issues/29) **closed 2026-09-29 (PR #53)** | Strip machine-specific references; infra reproducible off the Spark |
| [#30](https://github.com/sudoTomas/trader/issues/30) **closed 2026-09-29 (PR #55)** | `trader doctor` preflight + shadow-only quickstart |
| [#31](https://github.com/sudoTomas/trader/issues/31) **closed 2026-09-29 (PR #36)** | Publish: Docker image on GHCR + PyPI (`uvx trader`) |
| [#32](https://github.com/sudoTomas/trader/issues/32) **closed 2026-09-29 (PR #57)** | Translate Linear/v1 provenance into readable rationale |
| [#33](https://github.com/sudoTomas/trader/issues/33) **closed 2026-09-29 (PR #56)** | README overhaul |
| [#34](https://github.com/sudoTomas/trader/issues/34) | Exit-review lane (decide-gated; after #1) |
| [#35](https://github.com/sudoTomas/trader/issues/35) | **Epic:** open-source release (MIT, public after cleanup; Python stays) |

## Filed 2026-10-07 (Confirm seat)

| GitHub | What |
|---|---|
| [#99](https://github.com/sudoTomas/trader/issues/99) **closed 2026-10-08 (PR #101, deployed)** | Confirm seat stage 1: Nemotron drafts, Kev confirms, journal-only (`confirm` column; retires `TRADER_DECIDE_PROVIDER=jev`) |

## Filed 2026-10-08 (model picker)

PRD [#102](https://github.com/sudoTomas/trader/issues/102); glossary `CONTEXT.md` ("Models"); ADRs `docs/adr/0001`, `0002`.

| GitHub | Slice | Blocked by |
|---|---|---|
| [#103](https://github.com/sudoTomas/trader/issues/103) | 1/6 Providers and encrypted keys on every surface | — |
| [#107](https://github.com/sudoTomas/trader/issues/107) | 2/6 Profiles, Role Chains and the Probe in a shared registry | #103 |
| [#104](https://github.com/sudoTomas/trader/issues/104) | 3/6 Native Anthropic adapter | #103 |
| [#105](https://github.com/sudoTomas/trader/issues/105) | 4/6 Live layered pricing (local models are free) | #103 |
| [#108](https://github.com/sudoTomas/trader/issues/108) | 5/6 Per-Role daily Spend caps | #107, #105 |
| [#106](https://github.com/sudoTomas/trader/issues/106) | 6/6 Desk Models page | #107, #108 |

Batch one (2026-09-29) merged as PRs #36 (D), #39 (B), #38 (C), #37 (E), #40 (A); report in
`docs/reports/2026-09-29-batch-one.md`. Deployed only after the operator restarts `trader-api`.

2026-09-30: #70 (Ollama grammar cap, reported by HermeticOrmus) + #67 (sentiment cadence across restarts) merged as PR #72;
#20 epic closed; exit-review lane (#34) live since 14:00 UTC on v8 wording after runs 16/17/18.

Batch four (2026-09-29/30): #63 shared xAI manager (PR #64), #34 exit-review lane built dark + replay
flag (PR #65; issue stays open through the gate), #9 + #10 Grok crypto-flows seat and citation report
(PR #66). Briefs lane flipped live 2026-09-29 after the neutral replay (`docs/reports/2026-09-29-briefs-replay-gate.md`).
GLD/SLV/USO in per-symbol shadow since 2026-09-29 19:35 UTC.

Batch three (2026-09-29): #8 (PR #58), #4 (PR #59), #7 probe capability (PR #61, issue open until the
matrix is recorded), #5 (PR #60), fixes lane (PR #62: single-source version, FRED door for #17, replay
lane flags for #1, mark-vol cache). New: [#63](https://github.com/sudoTomas/trader/issues/63) shared
xAI credential manager.

Batch two (2026-09-29): #27, #29, #30, #33, #32 merged as PRs #54, #53, #55, #56, #57. Epic #35's remaining
items are operator-only: PyPI trusted publisher for `trader-agent`, flip visibility, tag `v0.1.0`.

Decisions recorded 2026-09-29: MIT; no TypeScript rewrite; Grok desk (#20) follows #21 and
precedes the distribution tail; #6 closed as deferred.

Milestone: [M5 Expansion](https://github.com/sudoTomas/trader/milestone/1).

## Done in Linear (not re-filed)

M0–M4 plus the money-path/desk work that earned its flip: EAI-161…180 (except 173/176
leftovers above), EAI-186 slice (SPY in universe), EAI-188 replay harness, EAI-190
desk, EAI-193 Robinhood no-go, EAI-194 LAN auth, EAI-195 operator doors (approve/adopt
surfaces exist; redeem is #11).
