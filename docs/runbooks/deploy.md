# Runbook: how the trader is deployed on this box

Two pieces, both reboot-safe since 2026-08-25:

1. **Postgres** — compose service, `restart: unless-stopped`:
   `docker compose -f infra/docker-compose.yml up -d db` (listens on **:5544**).
2. **API** (uvicorn :8080 — decide loop, RiskGuard, research desk) — a **systemd user
   unit**, NOT the compose `app` service (that containerized path exists but is not the
   deployed shape). The unit lives at `~/.config/systemd/user/trader-api.service`, runs
   `uv run uvicorn trader.surfaces.api.app:app --port 8080` from the repo checkout, and
   reads `.env` from the working directory. Linger is enabled for the user, so it starts
   at boot without a login session.

```bash
systemctl --user status trader-api        # is it up
systemctl --user restart trader-api       # deploy: git pull, then this
journalctl --user -u trader-api -f        # logs (structlog + uvicorn)
systemctl --user stop trader-api          # take the API down (loop stops cleanly)
```

**Deploying a change**: `git pull` (or commit) in `~/repos/trader`, run the suite, then
`systemctl --user restart trader-api`. Boot order is self-healing: the app fails closed
if the db is unreachable and the unit retries every 10 s until migrations apply.

**Credentials on this box**: Alpaca paper keys + `TRADER_XAI_TOKEN_KEY` in `.env`
(never committed); the encrypted xAI OAuth lineage at `~/.config/trader/xai_token.enc`.

**Invariants**: shadow mode is controlled by `TRADER_SHADOW_MODE` in `.env` — flipping
it requires a restart and is a deliberate operator act (see
`docs/runbooks/first-live-paper-trade.md`). `TRADER_ALPACA_LIVE_ENABLED` stays unset.
