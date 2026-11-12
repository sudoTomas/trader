# Runbook: the decide-change gate (EAI-188)

Every change to decide behavior — a prompt edit, a model swap, a new prompt lane, the
desk (EAI-190), a new asset class's first live symbol — goes through the same three
stages, in order. This formalizes the discipline that protected the money path in v1
(EAI-128): **one gate at a time, never two overlapping.**

## 1. Replay eval (offline; paid Jev calls when a confirm route is set)

A replay never trades, but it calls the decide model, and Jev on every open or close Draft
when a `confirm` route exists: those Jev calls are paid. The summary's `cost_usd` sums both.

Replay a representative window under the current config (baseline) and under the
candidate, then compare:

```bash
trader eval run --symbol BTC/USD --start 2026-08-18T00:00:00+00:00 \
    --end 2026-08-25T00:00:00+00:00 --label baseline
# ...apply the candidate change...
trader eval run --symbol BTC/USD --start 2026-08-18T00:00:00+00:00 \
    --end 2026-08-25T00:00:00+00:00 --label candidate
trader eval list
trader eval show <id>          # summary + per-step decisions
```

Replays run the live deterministic stop (#74, volatility-scaled): a replayed position past
its stop is closed at that step before the model is asked, and its cooldown keeps later
opens out of the crude book (`stops` / `stop_cooldown_denied` in the summary). A step's bar
close stands in for the live loop's two 60 s ticks. They also run the min hold: a model
close of a position the replay opened (or a seed with `opened_at`) less than
`TRADER_RISK_MIN_HOLD_SECONDS` before leaves the book as it is (`min_hold_denied`; the step
is marked `min_hold`), and the no-trade band: an open below `TRADER_RISK_NO_TRADE_BAND_FRAC`
of the run's equity (the Sleeve when `sleeve_usd` is set) never enters the book
(`no_trade_band_denied`; the step is marked `no_trade_band`). The earnings blackout
(`TRADER_RISK_EARNINGS_BLACKOUT_DAYS`) keeps a stock's opens out of the book from the
session before its report through the report session, read from the calendar row the
OpenBB producer had written by that step; a step with no row yet fails open, as live
(`earnings_blackout_denied`; the step is marked `earnings_blackout`).

Every replayed fill pays the cost model (#139, `TRADER_REPLAY_*`): half-spread (5 bp
equities/ETFs, 10 bp crypto, while replay has no real quote), a 2 bp fee, Alpaca's 25 bp
crypto taker fee, and square-root impact. The summary carries `gross_pnl`, `trading_costs`
(`spread` / `fee` / `impact` / `total`), `net_pnl` and the `cost_model` it ran with; each
fill step carries its own `fill_cost`. `realized_pnl` / `unrealized_pnl` stay gross, and
`cost_usd` is still the model calls (decide, and Jev when a confirm route is set). Compare
candidates on `net_pnl`: a candidate that trades more pays more.

Runs execute in the background (`POST /eval/replay`, MCP `run_replay_eval`); bars come
from the read-through cache (first run over a window backfills it), the decider is the
same code the live loop runs, and results land in `eval_runs`/`eval_steps` — **never**
`decisions`, never near an order path. Windows are capped at 500 steps; cost is ~zero on
the local Nemotron decide role.

What to compare in the summaries: `actions` mix (a candidate that opens 3× as often is a
different animal), `errors`, and the crude PnL, gross and net — which is **mark-to-bars
on one position: it ranks configurations, it does not predict live PnL** (the summary says
so in `pnl_model`). Replayed contexts currently exclude the Macro View lane and use the
`replay` trigger; judge relative differences, not absolutes.

**A model swap needs no Profile change**: `trader eval run --model provider:model` (API
`model=`, MCP `run_replay_eval(model=...)`) replays every decide call of that run on one
Provider and model from the model picker, the same `provider:model` syntax as
`trader profile set`. The client is built exactly as a Chain entry's, so a keyless `xai`
Provider signs with the service's SuperGrok login (the replay runs inside `trader-api`,
where that login lives). Unset, decide routes as the live loop's would (the active
Profile, else `.env`). The calls are metered into `usage_daily` under `decide` and held
to its Spend cap like any replay; the run's `config.model` records the spec; an unknown
Provider or one that cannot decide (Jev) is refused with a 422 before a run exists. The
desk's analysts and critic keep their own routes. Compare two models on the same window:

```bash
trader eval run --symbol ETH/USD --start 2026-09-13T00:00:00+00:00 \
    --end 2026-09-20T00:00:00+00:00 --label g47 --model grok-deep:grok-4.7
trader eval run --symbol ETH/USD --start 2026-09-13T00:00:00+00:00 \
    --end 2026-09-20T00:00:00+00:00 --label g43 --model grok:grok-4.3
```

**The desk (EAI-190) has first-class variant plumbing**: `trader eval run --desk` (API
`desk=true`) replays the same window through analyst fan-out + critic while the live desk
stays dark. The summary gains a `desk` tally (`approve/shrink/veto/pass_through/skipped`)
and each step's decision records the full desk verdicts. Compare desk-on vs desk-off on
the SAME window: the desk earns its flip if agreement-where-it-matters improves — fewer
bad opens and kept exits — not merely a different PnL number. A desk-on replay with the
critic on Grok costs real money per intent step; the hold-skip keeps it small, but check
`/usage` after.

### Exit-review lane (#34)

`TRADER_EXIT_REVIEW_ENABLED` (dark) adds a **held-position block** to the decide prompt
whenever a position is held: entry price, position age, unrealized P&L, the latest X
sentiment for that symbol, and an explicit ask to state whether the thesis still holds:
`close` (full, or partial via a smaller `notional_usd`) when it does not, `hold` when it
does. The ask is deliberately untilted: the earlier "prefer `close` ... a hold must justify
staying in" wording closed positions on noise-sized dips
(`docs/reports/2026-10-22-bot-1-exits.md`). The system prompt gains a matching description of `close`/reduce only when the block
is present. Advisory: RiskGuard never sees it (asserted structurally in
`tests/test_exit_review.py`).

Replay starts flat, so the harness **seeds** a held position for the window
(`--seed-qty` / `--seed-entry`; an in-memory book for the crude sim only, never a
`positions` row). Seed the SAME position into both runs so only the block differs; the
crude PnL then starts from that seed (a close realizes against the seeded entry, a run that
never closes carries the seed's mark-to-bars move as `unrealized_pnl`). The two commands
for the ETH window the briefs gate used (run them one after the other — two replays at
once 429 Alpaca), seeded with the book's ETH position — take `qty` / `avg_entry_price`
from `trader positions` at the time of the run, not from memory (the values below are the
2026-09-29 book):

```bash
trader eval run --symbol ETH/USD --start 2026-09-13T00:00:00+00:00 \
    --end 2026-09-27T00:00:00+00:00 --seed-qty 0.3779 --seed-entry 2584.0 \
    --no-exit-review --label exit-review-off
trader eval run --symbol ETH/USD --start 2026-09-13T00:00:00+00:00 \
    --end 2026-09-27T00:00:00+00:00 --seed-qty 0.3779 --seed-entry 2584.0 \
    --exit-review --label exit-review-on
```

(API: `POST /eval/replay?...&exit_review=true&seed_qty=0.3779&seed_entry=2584`; MCP:
`run_replay_eval(exit_review=true, exit_review_seed={"qty": 0.3779,
"avg_entry_price": 2584.0})`. `--seed-opened-at` dates the position for the age line;
default is the window start. Unset `--exit-review` follows the setting, i.e. off.)

What to compare: **kept exits** (`close` in `actions`, and whether they land on the steps
where P&L or sentiment turned against the seed) and the **hold rate**, before the crude
PnL. Every step's decision records the `held_position` entries the decider saw
(`trader eval show <id>`), so a close can be read against the block that prompted it. A
candidate that closes on every dip is as wrong as one that never closes: the lane earns
its shadow window if exits track the thesis, not the noise.

### Confirm veto (Jev)

Replays run the confirm stage live trading does: when the process registry has a `confirm`
route, every open or close Draft of a replay is sent to Jev on that route (a `--model` run
pins decide only; Jev keeps its own route), before the desk's critic. Each step's decision
records Jev's judgments under `confirm` (`answers`, `would_veto`, `veto_reason`,
`vetoed`, `verdict`); no confirm route records nothing, and a `--blind` run never sends Jev
the state and records `skipped: "blind"`. The veto is a replay parameter, not the live
`TRADER_CONFIRM_VETO_ENABLED`: `--confirm-veto` (API `confirm_veto=true`, MCP
`run_replay_eval(confirm_veto=true)`) turns an open the rule vetoes into a hold
(`confirm veto: <reason>`, the book stays flat); off (the default) the open stands and the
record says `would_veto`. Every run's `config.confirm_veto` says which it was.

The paired check, before the live veto is switched on: replay the same window for each of
Bot #1's symbols twice, veto off then veto on (one after the other, as above). Each run makes
one Jev request per open or close step, so state the request count (the off run's opens plus
closes, twice) and the cost before running it, and keep the exact commands:

```bash
trader eval run --symbol ETH/USD --start 2026-10-29T00:00:00+00:00 \
    --end 2026-11-12T00:00:00+00:00 --label confirm-veto-off --no-confirm-veto
trader eval run --symbol ETH/USD --start 2026-10-29T00:00:00+00:00 \
    --end 2026-11-12T00:00:00+00:00 --label confirm-veto-on --confirm-veto
```

What to compare: how many opens were vetoed (steps whose `confirm.vetoed` is true in the on
run; the off run's `would_veto` opens should be the same steps until the books diverge),
the `actions` mix, and `net_pnl` on and off. It informs the flip; it is not a gate in code.

## 2. Shadow window (live inputs, no orders)

Only if the replay comparison favors the candidate: run it against live data without
execution. For a new symbol/asset class that means shadow mode for that symbol — add it
to `TRADER_LOOP_SYMBOLS` **and** `TRADER_SHADOW_SYMBOLS` in the same edit, restart, and
leave it there until the journal earns its flip (never list a new ticker in
`TRADER_LOOP_SYMBOLS` alone: that is the flip); for a
prompt/model/desk change it means the change enabled with `TRADER_SHADOW_MODE=true` (or
the feature's own shadow flag). Hold the window until you have enough journaled decisions
to read (≥3 sessions for a venue-gated symbol; ~a week for a feature like the sentiment
trigger).

## 3. The flip (deliberate, exclusive)

Flip the config, restart the API (`systemctl --user restart trader-api` for the systemd
shape, `docker compose -f infra/docker-compose.yml up -d app` for compose — see
`docs/runbooks/deploy.md`), and verify `trader loop status` + `trader broker reconcile`. **Never flip while another gate is
open** — a new asset class, the sentiment trigger, and a desk change must not land in the
same window; if two are ready, sequence them and watch the journal between flips.

Rollback is always config-only: revert the flag, restart. RiskGuard is unchanged by any
of this — it was never the throttle being gated.

## Watching a shadow window

`scripts/desk-watch.sh` (hourly via `infra/systemd/trader-desk-watch.timer` +
`.service`; install per `docs/runbooks/deploy.md`) tails the journal for the events worth
seeing the day they happen — first drafted intent, critic vetoes, decide errors, an
`executed` row (which must never appear mid-window), critic spend nearing its cap. It
reads `TRADER_DATABASE_URL` from the environment or `.env` and works with host `psql` or
`docker exec` into the compose db. Findings append to `desk-watch.log` in `TRADER_REPO`
(gitignored) and the systemd journal (`journalctl --user -t trader-desk-watch`).
Monitoring only — flips stay manual.
