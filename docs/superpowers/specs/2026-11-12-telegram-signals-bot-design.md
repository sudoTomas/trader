# Telegram signals: a scorecard, then a Bot that trades the calls

Date: 2026-11-12. Status: agreed in conversation, pending this document.

## Why

Tomas follows CryptoJack's **Chart Chasers 🔔 ALERTS 🔔** Telegram channel, which posts crypto
trade calls. He wants a Bot dedicated to those calls, reading them from his own Telegram account
by polling. The channel only advertises its winners, so the first job is to find out whether the
calls make money before any of them is traded.

## What the channel looks like (read 2026-11-12, 357 posts since 2025-09-03)

- About 42 calls since late February 2026, roughly one a week. Free text with a mostly regular
  layout: `My Entry: 0.08054`, `TP1: … TP2: … TP3: …`, `Stop Loss: …` (often "I'm not setting
  one"), allocation ("0.5% of my deposit") and leverage ("7x"). Usually a chart image attached.
- Calls are futures: mostly longs, some shorts. Coins named: ADA, BCH, BTC, DOGE, ETH, HYPE,
  ONDO, PENGU, SOL, WLD, ZEC. Alpaca paper crypto (spot, long-only, unleveraged) lists all but
  PENGU, WLD and ZEC.
- Updates are replies to the call: "TP1 HIT", "+49%", "took partial profits and moved to
  break-even". Posts are edited after the fact (nearly every post carries an edit date).
- The rest: market commentary, "100 🔥 and I'll reveal the coin" teasers, coin reveals without
  levels ("I bought ONDO spot"), and exchange referral promotions.
- The linked discussion group **CryptoJack Chat** carries no usable calls and is ignored.

## Goals

**Phase 1, the scorecard (build now):**
- Poll the channel from the Spark every few minutes with Tomas's Telegram session, read-only
  (never send, never mark read). Store each post verbatim with its id, date, edit date and
  reply-to id; re-read recent posts so edits are captured.
- A model turns each post into exactly one record: a **call** (symbol, side, entry, TP1..TP3,
  stop or none, allocation %, leverage), an **update** to an earlier call (TP hit, partial profit,
  stop moved, closed, with the call it replies to), or **other** (commentary, teaser, promotion,
  image-only). The parse is journaled with the post and the model that produced it.
- Score every call against real price bars at 1x, unleveraged, after the replay cost model
  (#139): which of TP1/TP2/TP3/stop came first, the return, the time taken. A call with no stop
  is scored with the stop this Bot would use (RiskGuard's stop-loss fraction). Calls the Bot
  could not trade (shorts, unlisted coins) are scored in a separate column.
- Backfill: parse and score every past call in the channel.
- The scorecard is visible on all three surfaces (API, MCP, CLI) and in the desk.

**Phase 2, paper trading (build only after the gate passes and Tomas approves):**
- A new Bot (`telegram-chartchasers` or similar) with a $10,000 Sleeve trades the tradeable calls
  on Alpaca paper through the normal RiskGuard chain.
- Tradeable = long, coin listed on Alpaca, unleveraged. Shorts, unlisted coins and teasers are
  recorded and scored, never traded.
- Size = the call's allocation × its leverage, as a share of the Sleeve, unleveraged (0.5% at 7x
  → 3.5% of $10,000 = $350). RiskGuard's limits still apply.
- Exits follow the call: a third off at each TP; the stop moves to break-even after TP1 or when
  an update says so; a "closed" update closes the rest. RiskGuard's stop always applies, even
  when the call says "no stop loss".
- **Two Bots may hold the same symbol** (Tomas's choice): Bot #1 owns ETH/USD today and the
  channel calls ETH, so the Owner rule changes from "one live Bot per symbol" to per-Sleeve
  positions on a shared symbol. This touches RiskGuard, reconciliation (sleeve drift) and the
  executor, and is designed in Phase 2, not before.

## The gate (Phase 1 → Phase 2)

Over the backfilled calls plus at least four live weeks, the **tradeable** calls together make
money at 1x after costs, and at least half reach TP1 before the stop. The scorecard is reported
to Tomas, and he approves the switch. The gate is a report, not code.

## How a call is scored (Tomas's answers to the builder check)

- **Result that counts:** simulate the Phase 2 exits bar by bar: a third off at each TP, the stop
  to break-even after TP1 or when an update says so, the rest closed on a "closed" update. That
  P&L, after costs, is the gate's "makes money". The simpler TP1-or-stop-first outcome is
  reported alongside, and "≥ half reach TP1 before the stop" uses it.
- **Entry fill:** market, at the open of the next 15-minute bar after the post appears (what a
  polling Bot gets). The stated entry and the slippage from it are reported.
- **Backfill:** counts toward the gate. The parser takes only the call's levels (coin, side,
  entry, TPs, stop, allocation, leverage) from the final edited text and ignores any outcome text
  an edit added; prices decide the outcome. Live calls use the text as first seen; later edits
  are stored as versions.

## Calls made after the builder check (Claude's)

- **Stop:** the tighter of the call's stop and RiskGuard's fixed 8% (`TRADER_RISK_STOP_LOSS_FRAC`,
  not the volatility-tightened value); a call with no stop uses 8%. In the simulation the stop
  also moves to break-even per the ladder.
- **Bars:** Alpaca crypto 15-minute bars, fetched on demand. When a bar touches both a TP and
  the stop, the stop counts first (pessimistic). Unlisted coins (PENGU, WLD, ZEC) are scored
  only if another source has their bars, and never count toward the gate.
- **Parse model:** a new `signals` Role, initially `grok-none:grok-4.3` then
  `openrouter:deepseek/deepseek-v4.1-flash`, Spend cap $2/day.
- **Telegram session on the Spark:** its own session, created by a QR login run on the Spark
  (`trader`-side command or script), stored encrypted with `TRADER_SECRETS_KEY`. It does not
  copy the Mac's session: one session used from two hosts at once can be revoked by Telegram.
- **Scope of the first plan:** Phase 1 only. Phase 2 (the Bot, the shared-symbol Owner change,
  RiskGuard and executor work) gets its own design after the gate passes.

## Non-goals

- No orders in Phase 1.
- No reading of CryptoJack Chat or any other chat.
- No sending, reacting or marking read on Telegram.
- No image understanding: an image-only post is "other".
- No leverage, no shorts on Alpaca, no inverse products for this Bot.
- No change to Bot #1.

## Anti-goals

- A post parsed into a trade it didn't state (inventing an entry or a stop). When a level is
  missing, the field is null and the score says how it was filled.
- Scoring only the calls the channel later celebrated: every call is scored from its own post.
- The Telegram session (full account access) in the repo, logs, the journal or a prompt.

## Constraints

- Prime directive and invariants in CLAUDE.md hold: the parse proposes nothing that reaches the
  broker in Phase 1; in Phase 2 every order goes through RiskGuard; paper only.
- No market-data storage in the live path: the scorer fetches bars on demand.
- One service layer, three surfaces.
- The Telegram session is a full-account credential. On the Mac it is the keychain item
  `telegram-session` (with `telegram-api-id`, `telegram-api-hash`); on the Spark it is stored
  encrypted like Provider keys (`TRADER_SECRETS_KEY`), never in `.env` as plain text.

## Decided by Tomas

Source: Chart Chasers ALERTS only. Purpose: scorecard first, then trade. Misfits: longs only, 1x,
always a stop. Exits: follow the call's TPs and updates. Sizing: mirror the channel (allocation ×
leverage of the Sleeve). Gate: positive net at 1x after costs and ≥ half reach TP1, over the
backfill plus four live weeks. Shared symbols: both Bots may trade the same symbol. Sleeve:
$10,000.

## My calls (Claude's)

- Polls from the Spark inside the trader process (or its supervisor), not a separate service;
  cadence a few minutes, configurable.
- Telethon, the same client the login used.
- The parse uses the model picker: a new Role (e.g. `signals`) with its own Chain and Spend cap,
  so the model can change without a deploy. Structured JSON output, validated; a malformed parse
  is stored as "unparsed", never guessed.
- Updates attach to their call by Telegram reply id first, then by symbol and recency.
- Phase 1 stores posts and parses in their own tables; the scorer reads them point-in-time.

## Left to the builder

- Table shapes and migration; whether polling lives in the supervisor or a research collector
  (`src/trader/research/` has the pattern).
- The parse prompt's wording and schema, tested on the stored backfill.
- How bars are fetched for unlisted coins' scores (another data source, or marked unscorable).
- The scorer's cost model parameters for crypto (reuse #139's).
- The desk view.
