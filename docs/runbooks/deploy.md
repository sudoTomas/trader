# Runbook: deploying the trader

Two pieces: **Postgres 16** and the **API** (uvicorn on `:8080` — decide loop,
RiskGuard, research desk, MCP at `/mcp`). Both shapes below are supported; pick one.
The box the project was developed on is documented separately in
[`docs/deploy/dgx-spark.md`](../deploy/dgx-spark.md) — read it for the specifics of
*that* host, not for anything the repo requires.

## Prerequisites (either shape)

- Docker with compose v2 (for Postgres; and the app, in shape A).
- A `.env` at the repo root, from `.env.example`. At minimum: `TRADER_API_KEY` (or
  `TRADER_AUTH_DISABLED=true` for a private dev box), Alpaca **paper** keys, and a
  decide model — either `TRADER_DECIDE_BASE_URL`/`TRADER_DECIDE_MODEL`/`TRADER_DECIDE_API_KEY`
  for any OpenAI-compatible endpoint, or a self-hosted Nemotron vLLM at
  `TRADER_NEMOTRON_BASE_URL` (the preset; unauthenticated endpoints belong on a LAN only).
  `TRADER_ALPACA_LIVE_ENABLED` stays unset.
- Research roles (brief/compiler/critic/sentiment) need an xAI credential
  (`trader xai login` or `XAI_API_KEY`) or `TRADER_RESEARCH_*`; without one they
  degrade honestly, they do not block the money path.

## Shape A — compose (reference)

```bash
docker compose -f infra/docker-compose.yml up -d            # pulls ghcr.io/sudotomas/trader:latest
docker compose -f infra/docker-compose.yml up -d --build    # or build from this checkout
curl -s localhost:8080/health                               # {"status":"ok","db":"ok"}
docker compose -f infra/docker-compose.yml logs -f app      # structlog + uvicorn
```

The `app` container reaches Postgres as `db:5432` and a host-local LLM as
`host.docker.internal` (see the comments in `infra/docker-compose.yml`). **Deploy a
change**: `git pull`, then `up -d --build` (or `pull` + `up -d` for a released tag).
Migrations run at boot; the app fails closed and restarts until the db is healthy.

## Shape B — host systemd user unit over the compose db

Runs uvicorn from the checkout with `uv`, which keeps `trader …` CLI, tests, and the
service on one interpreter. Templates live in [`infra/systemd/`](../../infra/systemd);
they assume the checkout at `~/repos/trader` and `uv` at `~/.local/bin/uv` — edit the
two paths if yours differ (the comments say how).

```bash
docker compose -f infra/docker-compose.yml up -d db          # Postgres on :5544, reboot-safe
uv sync --all-extras --dev
mkdir -p ~/.config/systemd/user
cp infra/systemd/trader-api.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now trader-api
loginctl enable-linger "$USER"                                # start at boot without a login

systemctl --user status trader-api        # is it up
journalctl --user -u trader-api -f        # logs
systemctl --user restart trader-api       # deploy: git pull + run the suite, then this
systemctl --user stop trader-api          # take the API down (loop stops cleanly)
```

Optional shadow-window watcher (monitoring only, see
`docs/runbooks/decide-change-gate.md`): copy `trader-desk-watch.service` and
`trader-desk-watch.timer` the same way and `enable --now trader-desk-watch.timer`.
`scripts/desk-watch.sh` reads `TRADER_DATABASE_URL` from the environment or `.env`
and uses host `psql` when present, otherwise `docker exec` into the compose db.

## OpenBB sidecar (optional, #141)

OpenBB (Apache-2.0) feeds the `earnings`, `options` and `fundamentals` Sources and the
RiskGuard earnings blackout. It runs as its own `openbb-api` process on `:6900` in its own
venv or container, with pinned versions (`infra/openbb/requirements.txt`: the equity,
derivatives and news routers; the keyless Nasdaq, Cboe, SEC and FINRA providers). The
trader never imports it: the producer calls its REST routes on a timer and appends compact
rows to `openbb_snapshots`. The trader runs normally without it: the three Sources are
absent (or age, flagged `STALE`) and the blackout fails open with a warning in its verdict.

Pick the shape that matches the trader's:

```bash
# A (compose): the sidecar as a container, opt-in by profile, loopback-published.
docker compose -f infra/docker-compose.yml --profile openbb up -d --build openbb
echo 'TRADER_OPENBB_BASE_URL=http://openbb:6900' >> .env       # the app container's view

# B (systemd): its own venv, never the trader's.
uv venv ~/.local/share/openbb/venv --python 3.12
uv pip install --python ~/.local/share/openbb/venv -r infra/openbb/requirements.txt
cp infra/systemd/openbb-api.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now openbb-api
echo 'TRADER_OPENBB_BASE_URL=http://127.0.0.1:6900' >> .env
```

Restart the trader so it reads the URL, check the sidecar answers, then run the producer
once by hand and install its timer (11:30 and 21:30 UTC, every day):

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  'http://127.0.0.1:6900/api/v1/equity/calendar/earnings?provider=nasdaq'   # 200 (204 = empty day)
trader openbb refresh                    # every Bot's US stocks; or --symbols AAPL,MSFT
trader openbb show --kind earnings       # one row per stock: next_report_date, report_time
cp infra/systemd/trader-openbb-refresh.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now trader-openbb-refresh.timer
```

`refresh` reports what it wrote (`written`), what `failed` per provider, and each provider's
breaker (three failures in a row skip that provider for 6 h; a restart clears them).
`status: unavailable` with "unreachable" means the sidecar is down: nothing was written and
no breaker was charged.

**Live check before relying on the blackout:** compare `trader openbb show --kind earnings`
for each of Bot #2's six stocks with the report dates on nasdaq.com's earnings calendar.
The blackout (`TRADER_RISK_EARNINGS_BLACKOUT_DAYS`, default 1, `0` = off) then denies
opens and adds from the session before each report through the report session; exits
always pass. Subscribing a Bot to the Sources is a decide-behavior change and goes through
`docs/runbooks/decide-change-gate.md`; Bot #2 first:

```bash
trader bot show bot-2-largecap   # its current Sources: --source replaces the whole list
trader bot version bot-2-largecap \
  --source sentiment --source brief --source held_position --source macro_view \
  --source earnings --source options --source fundamentals
```

**Upgrading OpenBB**: bump the pins in `infra/openbb/requirements.txt` together, rebuild
(or reinstall the venv), run `trader openbb refresh` and read `failed` before the timer
runs on it. Scraped endpoints change shape without notice; the breaker and the `STALE`
flags are what make a break visible.

## After any deploy

```bash
trader health              # or curl /health
trader loop status         # errors: 0; shadow matches TRADER_SHADOW_MODE
trader broker reconcile    # {"drift": {}} — drift pauses routing until resolved
trader risk status         # breakers track real equity, session day is today
```

## Invariants

- `TRADER_SHADOW_MODE` is read at boot; flipping it is a deliberate operator act and a
  restart (`docs/runbooks/first-live-paper-trade.md`), gated by
  `docs/runbooks/decide-change-gate.md`. So is a decide model swap.
- `TRADER_ALPACA_LIVE_ENABLED` stays unset. Nothing in either shape can construct a
  live client without it.
- `.env` is never committed and never baked into an image; the published image
  contains no credentials.
