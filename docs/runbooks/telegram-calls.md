# Runbook: Telegram channel calls, the scorecard (Phase 1)

The trader reads one Telegram channel (Chart Chasers ALERTS, `TRADER_CALLS_CHANNEL_ID`)
with Tomas's own account, read-only: it never sends, reacts or marks a post read. Each
post is stored verbatim with its edits as versions. The `calls` Role parses each post
once into a call, an update to an earlier call, or other. Every call is scored against
Alpaca 15-minute bars with the exit ladder (a third off at each TP, the stop to break-even
after TP1, the rest closed on a "closed" update), after costs. Phase 1 places no orders.
Design: `docs/superpowers/specs/2026-11-12-telegram-signals-bot-design.md`.

Do the steps in order, on the Spark, from the trader checkout.

## 1. Install the extra

The reader needs Telethon and the login needs `qrcode`, both in the `telegram` extra:

```bash
uv sync --all-extras --dev
```

## 2. Log in on the Spark

The Spark gets its own Telegram session. Do not copy the Mac's: one session used from
two hosts at once can be revoked by Telegram.

```bash
trader telegram login --name spark
```

It asks for `api_id` and `api_hash` (from my.telegram.org; on the Mac they are the
keychain items `telegram-api-id` and `telegram-api-hash`), then prints a QR code. On the
phone: Telegram → Settings → Devices → Link Desktop Device, and scan it. The code renews
every 60 seconds. If the account has a 2FA password, it asks for it next.

The session is stored in `channel_sessions`, sealed with `TRADER_SECRETS_KEY` (the login
refuses when it is unset). It is a full-account credential: it is never written to
`.env`, logged, journaled or put in a prompt. Check it:

```bash
trader telegram status        # session: true, and the account's first name
```

`spark` is the default `TRADER_CALLS_SESSION_NAME`; a session under another name needs
that setting too.

## 3. Set the `calls` Role's Chain and cap

```bash
trader profile set everyday calls \
  --model grok-none:grok-4.3 --model openrouter:deepseek/deepseek-v4.1-flash
trader cap set calls 2        # $2 per UTC day
trader cap list               # the cap, and today's spend
```

`profile set` Probes each entry with a synthetic channel post. This is the first live
check that each model accepts the parser's nested JSON schema. If a Probe fails, that
model cannot serve the Role: pick another rather than forcing it. At the cap, the Role's
paid models sit out the rest of the UTC day; a free one still serves.

## 4. Switch it on

In `.env`:

```bash
TRADER_CALLS_ENABLED=true
```

Restart outside market hours, so no wakeup is cut short:

```bash
systemctl --user restart trader-api
journalctl --user -u trader-api -f     # calls_connected; no calls_poller_not_started
```

The API boots without waiting on Telegram: the poller connects in the background and
polls every `TRADER_CALLS_POLL_SECONDS` (default 300). A failed connect
(`calls_connect_failed`) is retried after 5 seconds, doubling up to the poll interval, and
a dropped connection is reconnected before the next pass. While it is not connected the
stored posts, calls and scorecard still answer, and `trader calls poll` and
`trader calls backfill` answer 503. The scorecard's `poller` shows the state:
`connected`, `last_ok` (the last good pass) and `last_error` (the error type of the last
failed connect or pass). With no stored session (or `TRADER_SECRETS_KEY` unset), it logs
`calls_poller_not_started` and every calls command answers 409. With no Alpaca keys,
posts are stored and parsed but not scored (`calls_scoring_off`).

Every pass ends in a parse, link and score, so posts left unparsed (the cap, a model
error) are picked up on the next pass. Open calls, and calls whose bars fetch failed,
are rescored at most once an hour; a coin Alpaca has no bars for stays unscorable until
the next backfill.

## 5. Backfill, once

```bash
trader calls backfill
```

It stores the channel's whole history as backfilled and returns
`{"backfill": {"stored": n}, "processing": "started"}` (`calls_backfill_stored` in the
log). The parse (levels only: outcome text an edit added later is ignored), the links and
the rescore of every call then run in the background; `calls_process_done` in the log,
with the counts, marks the end. Watch it fill in:

```bash
trader calls scorecard        # calls and scored counts grow as the parse goes
journalctl --user -u trader-api -f | grep -E 'calls_(backfill_stored|process_done|parse_stopped)'
```

A second backfill while that runs answers `"processing": "already running"`. A parse
stops early on a model error or at the cap (`calls_parse_stopped`) and leaves the rest
pending; the poller's next pass carries on with it. Live polling stores nothing until
the backfill has stored something, so posts read before it are not counted as live. Run
it once. Every write is idempotent, so a second run does no harm.

## 6. Check

```bash
trader telegram status
trader calls posts            # newest posts and the kind each was parsed as
trader calls list             # calls: levels, score, linked updates
trader calls scorecard        # the scorecard and the gate
trader calls poll             # poll now instead of waiting for the next pass
```

(`GET /calls/posts`, `GET /calls`, `GET /calls/scorecard`, `POST /calls/poll`,
`POST /calls/backfill`; the same capabilities on MCP.) In the desk, the Bots page has a
Telegram calls panel with the same scorecard.

## 7. The gate

`gate_pass` in the scorecard is true when all of these hold:

- the backfill has run, and at least 28 days have passed since the first live post;
- the tradeable calls (long, with Alpaca bars for `<COIN>/USD`) make money together:
  sleeve-weighted net P&L above zero, after costs, at 1x, on a $10,000 sleeve
  (`TRADER_CALLS_SLEEVE_USD`; size per call = sleeve × allocation × leverage, 1% and 1x
  when the call omits them);
- at least half of the resolved tradeable calls reach TP1 before the stop.

`gate_reasons` lists what is still missing. The gate is reported, not enforced: nothing
trades on it. When it passes, the scorecard goes to Tomas and he decides.

Phase 2 (a Bot that trades the calls on paper, and two Bots holding the same symbol)
needs its own design before any code. Nothing in this feature places an order.

## Revoke the session

On the phone: Telegram → Settings → Devices, and end the Spark's session. The poller then
fails its passes or its reconnects (`calls_poll_failed` or `calls_connect_failed` in
the log, `poller.last_error` in the scorecard) until a new `trader telegram login` and a restart.

## Turn it off

Set `TRADER_CALLS_ENABLED=false` in `.env` and restart `trader-api`. The poller stops and
the calls commands answer 409. The stored posts, parses and scores stay. Revoke the
session too if it is no longer wanted.
