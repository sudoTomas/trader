# Quickstart — shadow-only, under 10 minutes

The smallest working profile: Postgres in Docker, Alpaca **paper** keys, one
OpenAI-compatible decide endpoint (or the Nemotron preset), shadow mode. The loop wakes,
builds context from live market data, asks the model for a decision, runs it through
RiskGuard, and **journals it without submitting anything**. No xAI credential is needed —
research roles fall back to the decide model and X sentiment stays off.

Live trading is impossible in this profile: `TRADER_ALPACA_LIVE_ENABLED` stays unset, and
`TRADER_SHADOW_MODE=true` means even the paper account never sees an order.

## 0. What you need (2 min)

- Docker (for Postgres), Python 3.12, and [`uv`](https://docs.astral.sh/uv/).
- Alpaca **paper** API keys — Alpaca dashboard → *Paper Trading* → *API Keys*. Paper keys
  are separate credentials from live keys; the paper host is `paper-api.alpaca.markets`.
  The same keys serve market data (crypto is free; equities use the IEX feed).
- One decide endpoint, either:
  - **any OpenAI-compatible `/v1`** (OpenAI, Groq, Together, Ollama, vLLM, xAI) that
    supports `response_format: json_schema`, or
  - the **Nemotron preset**: a vLLM serving `nemotron-3-super` at
    `http://localhost:8009/v1` (the project's own box). The preset bakes in
    `chat_template_kwargs.enable_thinking=false`; nothing to configure.

```bash
git clone https://github.com/sudoTomas/trader && cd trader
uv sync --all-extras --dev
```

## 1. Postgres (1 min)

```bash
docker compose -f infra/docker-compose.yml up -d db
```

Expected — the container is healthy on `:5544`:

```
 ✔ Container trader-db-1  Healthy
```

## 2. `.env` (2 min)

```bash
cp .env.example .env
```

Edit `.env`. The example already has `TRADER_AUTH_DISABLED=true` and the Postgres URL;
every other line below is in it, commented — uncomment and fill. (Every variable is
described in [`docs/configuration.md`](configuration.md).)

```bash
# .env — shadow quickstart profile
TRADER_AUTH_DISABLED=true            # local dev only; set TRADER_API_KEY instead if the port is reachable
TRADER_DATABASE_URL=postgresql://trader:trader@localhost:5544/trader

ALPACA_API_KEY_ID=PK...              # PAPER keys
ALPACA_API_SECRET=...

TRADER_SHADOW_MODE=true              # decide + risk-check, submit nothing
TRADER_LOOP_ENABLED=true             # run the decide loop in the API process
TRADER_LOOP_SYMBOLS=BTC/USD,ETH/USD  # crypto trades 24/7, so a first run works at any hour

# Decide endpoint — pick ONE:
#   (a) any OpenAI-compatible /v1:
# TRADER_DECIDE_BASE_URL=https://api.openai.com/v1
# TRADER_DECIDE_MODEL=gpt-4o-mini
# TRADER_DECIDE_API_KEY=sk-...
#   (b) the Nemotron preset — leave TRADER_DECIDE_BASE_URL unset:
# TRADER_NEMOTRON_BASE_URL=http://localhost:8009/v1
```

Leave every `XAI_*` / `TRADER_XAI_*` line commented out, and leave
`TRADER_ALPACA_LIVE_ENABLED=false` exactly as the example has it.

## 3. `trader doctor` (1 min)

```bash
uv run trader doctor
```

It reads `.env` directly (not through the API) and probes each dependency once:
Postgres + pending migrations, one tiny schema-constrained completion on the decide
endpoint, `GET /v2/account` on the paper host, one latest quote for your first symbol,
the optional xAI credential, and the mode flags. Expected on a fresh database:

```
CHECK        STATUS    DETAIL
postgres     degraded  reachable; 7 migration(s) pending: 0001_baseline.sql, 0002_risk_state.sql, ...
decide       ok        openai_compat gpt-4o-mini 812 ms
broker       ok        paper account ACTIVE, equity $100,000.00
market_data  ok        BTC/USD bid 64000 / ask 64010 @ 2026-09-29T14:00:00+00:00
xai          degraded  no xAI credential — research roles (brief/compiler/critic) fall back to the decide model; X sentiment is off
mode         ok        shadow=on loop=on symbols=BTC/USD,ETH/USD desk=off briefs=off sentiment_trigger=off auth=disabled

fix:
  postgres     start the API — it applies migrations on boot (nothing was applied now)
  xai          set XAI_API_KEY, TRADER_XAI_TOKEN_KEY — optional — an API key, or a Fernet TRADER_XAI_TOKEN_KEY then `trader xai login`

result: ready → exit 0
```

Both `degraded` rows are expected here: migrations apply when the API boots (the doctor
never applies them), and xAI is optional. With the Nemotron preset the decide row reads
`nemotron nemotron-3-super 546 ms`.

`missing` on a required row exits 1 and names the env var to set — fix it and re-run.
`--json` gives the same report as a machine-readable object. Common misses:

| Row | Detail | Fix |
|---|---|---|
| `postgres` | `unreachable: connection refused` | step 1 didn't run, or `TRADER_DATABASE_URL` points elsewhere |
| `decide` | `nemotron nemotron-3-super: ... ConnectError` | nothing is serving `TRADER_NEMOTRON_BASE_URL`; set `TRADER_DECIDE_*` for a hosted endpoint instead |
| `decide` | `not configured: TRADER_DECIDE_MODEL is required` | you set the base URL without a model id |
| `decide` | `reply did not honor the JSON schema` (degraded) | the endpoint ignores `response_format`; decides will fail validation and retry once per wakeup — pick a model/provider with guided JSON |
| `broker` | `paper account rejected: ... HTTP 401` | live keys, or a typo — regenerate on the *Paper Trading* page |
| `market_data` | `unknown symbol 'X'` | only the seed universe is tradable: `BTC/USD`, `ETH/USD`, `SPY` |
| `mode` | `WARNING: TRADER_AUTH_DISABLED=true with a non-loopback API address` | set `TRADER_API_KEY` before exposing the port |

## 4. Start the API (1 min)

```bash
uv run uvicorn trader.surfaces.api.app:app --port 8080
```

Expected — migrations apply, then the server listens:

```
2026-09-29 14:02:01 [info     ] migration_applied              name=0001_baseline.sql
...
2026-09-29 14:02:01 [info     ] migration_applied              name=0007_authored_at.sql
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
```

A shadow build constructs **no broker object at all** — there is no venue client to
misuse, not merely a flag. RiskGuard sizes against `TRADER_SHADOW_EQUITY` (default
100,000) rather than the paper account; the doctor's broker row proved the keys, the loop
does not need them until shadow is off.

In a second terminal:

```bash
uv run trader health
```

```json
{"status": "ok", "service": "trader", "version": "...", "db": "ok"}
```

`uv run trader doctor` now reports `postgres ok` (`7 migrations applied, 0 pending`).

## 5. Wake the loop (1 min)

The loop heartbeats on its own (`TRADER_LOOP_TICK_SECONDS`, default 60 s); force a pass
now instead of waiting:

```bash
uv run trader wake
```

Expected — one wakeup per symbol, `status: shadow`, nothing submitted:

```json
{
  "woken": true,
  "results": [
    {"symbol": "BTC/USD", "triggers": ["heartbeat"], "status": "shadow", "decision_id": 1, "action": "hold", "reason": ""},
    {"symbol": "ETH/USD", "triggers": ["heartbeat"], "status": "shadow", "decision_id": 2, "action": "hold", "reason": ""}
  ]
}
```

`action` is whatever the model proposed. An `open` in shadow still runs the full
RiskGuard chain — `reason` then carries the chain's verdict text — and is journaled with
`status: shadow` (or `denied` if a check refused it). Either way the paper account is
untouched.

## 6. Read the journal (1 min)

```bash
uv run trader decisions --limit 3
```

Expected — one row per wakeup, newest first, with the exact prompt and raw model output
the decision was made from (prompts are long; the shape is what matters):

```json
[
  {
    "id": 2,
    "ts": "2026-09-29T14:03:11.402518+00:00",
    "symbol": "ETH/USD",
    "asset_class": "crypto",
    "trigger": "heartbeat",
    "status": "shadow",
    "prompt": "You are the decide step of ...",
    "raw_output": "{\"action\": \"hold\", \"conviction\": 0.35, \"rationale\": \"...\", \"side\": null, \"notional_usd\": null}",
    "decision": {"action": "hold", "conviction": 0.35, "rationale": "...", "side": null, "notional_usd": null},
    "risk_verdicts": null,
    "macro_view_version": null,
    "brief_version": null,
    "desk_verdicts": null,
    "cost_usd": 0.0,
    "latency_ms": 1840
  },
  ...
]
```

`risk_verdicts` is `null` for a hold (nothing to guard) and an ordered list of
`{check, verdict, reason}` entries for an open. Confirm nothing reached the venue:

```bash
uv run trader orders        # → []
uv run trader loop status   # → {"running": true, "shadow": true, "shadow_symbols": [], "stats": {"wakeups": 2, "decisions": 2, "errors": 0, ...}}
```

That is the whole shadow profile: leave the API running and it journals a decision per
symbol per heartbeat, for free on the Nemotron preset or at the endpoint's token price
otherwise (`uv run trader usage`).

## Where to go next

- **Feed it research**: `uv run trader note add --title "..." --body "..."` — notes
  compile into the Macro View the decide prompt reads (`docs/runbooks/research-capture.md`).
  Compilation uses the decide model until an xAI credential exists.
- **Add xAI (optional)**: `XAI_API_KEY=...`, or `TRADER_XAI_TOKEN_KEY` + `trader xai
  login` for SuperGrok OAuth. Grok then fronts research roles and enables X sentiment.
- **Trade on paper**: flipping `TRADER_SHADOW_MODE=false` is a deliberate operator act
  with its own runbook — `docs/runbooks/first-live-paper-trade.md`. Do not flip it in the
  first ten minutes.
- **Replay before changing the prompt or model**: `docs/runbooks/decide-change-gate.md`.
- The design: `docs/architecture.md`.
