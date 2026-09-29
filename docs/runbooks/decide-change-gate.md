# Runbook: the decide-change gate (EAI-188)

Every change to decide behavior — a prompt edit, a model swap, a new prompt lane, the
desk (EAI-190), a new asset class's first live symbol — goes through the same three
stages, in order. This formalizes the discipline that protected the money path in v1
(EAI-128): **one gate at a time, never two overlapping.**

## 1. Replay eval (offline, free)

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

Runs execute in the background (`POST /eval/replay`, MCP `run_replay_eval`); bars come
from the read-through cache (first run over a window backfills it), the decider is the
same code the live loop runs, and results land in `eval_runs`/`eval_steps` — **never**
`decisions`, never near an order path. Windows are capped at 500 steps; cost is ~zero on
the local Nemotron decide role.

What to compare in the summaries: `actions` mix (a candidate that opens 3× as often is a
different animal), `errors`, and the crude PnL — which is **mark-to-bars with no
costs/slippage: it ranks configurations, it does not predict live PnL** (the summary says
so in `pnl_model`). Replayed contexts currently exclude the Macro View lane and use the
`replay` trigger; judge relative differences, not absolutes.

**The desk (EAI-190) has first-class variant plumbing**: `trader eval run --desk` (API
`desk=true`) replays the same window through analyst fan-out + critic while the live desk
stays dark. The summary gains a `desk` tally (`approve/shrink/veto/pass_through/skipped`)
and each step's decision records the full desk verdicts. Compare desk-on vs desk-off on
the SAME window: the desk earns its flip if agreement-where-it-matters improves — fewer
bad opens and kept exits — not merely a different PnL number. A desk-on replay with the
critic on Grok costs real money per intent step; the hold-skip keeps it small, but check
`/usage` after.

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
