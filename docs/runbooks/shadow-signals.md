# Runbook: shadow signals, Kronos and crypto funding/OI (#144)

Two Sources are scored before any Bot reads them:

- **`kronos`**: a Kronos-small forecast (MIT, `shiyu-coder/Kronos`, 24.7M parameters) from
  a GPU sidecar. Per decide, the symbol's last 400 1h bars go in; N independently sampled
  close paths come back (kept apart, not averaged); the trader journals the share of paths
  up and the median, p10 and p90 return `TRADER_SIGNAL_HORIZON_BARS` (default 24) bars ahead.
- **`crypto_derivatives`**: BTC and ETH USDT-perp funding rate, open interest and its 24h
  change, and mark-vs-index basis from Binance, Bybit and OKX via ccxt public endpoints
  (no keys).

Both are fetched on use and journaled in `decisions.signals` on every decide of a Bot that
subscribes. Neither reaches the decide prompt unless that Bot's subscription is switched
on (`in_prompt`, off by default). Deploying this changes no decision.

## 1. Run the Kronos sidecar (operator step)

The sidecar is its own container; the trader never loads the model. The image clones
Kronos's model code at a pinned commit and downloads the weights at pinned Hugging Face
revisions (`infra/kronos/Dockerfile` build args), so the running container is offline.

On the GPU box, with a CUDA PyTorch base image:

```bash
KRONOS_BASE_IMAGE=nvcr.io/nvidia/pytorch:<tag>-py3 \
  docker compose -f infra/docker-compose.yml --profile kronos up -d --build kronos
curl -s localhost:8095/health
```

`/health` names the model, its revision and the device, and reports the process's peak
memory (`peak_rss_mb`). On a host without the NVIDIA container runtime, delete the
`deploy:` block of the `kronos` service (CPU inference works, slowly).

Acceptance checks:

- **Memory under 2 GB.** The compose service is capped at `mem_limit: 2g`; confirm with
  `docker stats --no-stream trader-kronos-1` and `/health` after a day of forecasts. On a
  unified-memory box (DGX Spark) the GPU allocations count against the same pool, so read
  `nvidia-smi` too.
- **Inference time per symbol logged.** The sidecar prints one JSON line per forecast
  (`"event": "kronos_forecast"`, `symbol`, `inference_ms`, `peak_rss_mb`;
  `docker logs trader-kronos-1`), the trader logs `kronos_forecast` with the same
  `inference_ms`, and the journal keeps it in `signals.kronos.reading.inference_ms`.

Then point the trader at it in `.env` and restart:

```bash
TRADER_KRONOS_URL=http://kronos:8095       # the app container
TRADER_KRONOS_URL=http://localhost:8095    # a host systemd unit
```

The sidecar is unauthenticated; its port binds to 127.0.0.1 only. When it is down the
decide goes on and that row journals no Kronos reading (`source_unavailable` in the log).

## 2. Subscribe

Funding/OI needs nothing configured (`TRADER_DERIVATIVES_VENUES` defaults to
`binance,bybit,okx`; a venue that is geo-blocked from the box just drops out of the
reading).

- **Bot #1**: `TRADER_SHADOW_SIGNALS_ENABLED=true` and restart. Bot #1's spec gains both
  subscriptions, journal-only (a new Bot #1 version, audited). Its prompt is unchanged.
- **A Bot defined in the store**: a new version whose Sources include `kronos` and/or
  `crypto_derivatives` (`trader bot create ... --source kronos --source crypto_derivatives`,
  or `new_bot_version`). Without `in_prompt` the readings are journaled only.

Check a few rows:

```sql
SELECT ts, symbol, signals FROM decisions WHERE signals IS NOT NULL ORDER BY ts DESC LIMIT 5;
```

## 3. Score

```bash
trader signal-scores --days 14            # every Bot's readings
trader signal-scores --days 14 --bot bot-1
```

(`GET /signals/scores?days=14`, MCP `get_signal_scores`.) Each reading is scored against
the close `TRADER_SIGNAL_HORIZON_BARS` 1h bars after it, fetched now and not stored; a
bar read by several Bots or wakeups counts once; readings whose horizon has not closed
are counted as `pending`.

- **Kronos**: `coverage` is the share of realized returns inside the forecast's p10..p90
  (calibrated is near 0.80); `hit_rate` is the share of calls (more or fewer than half the
  paths up) that got the direction right. Per symbol and overall.
- **Crypto derivatives**: rank IC (Spearman) of `funding_rate`, `oi_change_24h` and
  `basis` against the forward return, pooled across BTC and ETH and per symbol.

Hourly readings 24 bars ahead overlap, so `n` overstates the independent observations by
up to the horizon: 300 hourly readings are closer to 12 independent ones than to 300.

## 4. Decide, after two weeks

Write the report in `docs/reports/` from two weeks of shadow data: does Kronos's band
cover about 80% with a hit rate above one half on enough symbols, and does any
funding/OI factor carry an IC with a stable sign across both weeks? Only if one does,
propose switching a Bot on, through the decide-change gate (`decide-change-gate.md`):

```bash
trader bot signal bot-2-largecap kronos --in-prompt        # a new version, audited
trader bot signal bot-2-largecap kronos --no-in-prompt     # back off
```

(`POST /bots/{name}/signal-prompt {"source", "in_prompt"}`, MCP `set_signal_prompt`.)
Bot #1 follows Settings and is refused: it journals the shadow signals only.

## Replay

Kronos replays point-in-time: its bars come from the replay's provider, frozen at each
step, and the step records the reading under `signals`. Kronos was trained on public
market data up to its release, so a replay window before that is in-sample for it.
The derivatives Source reads nothing in replay: the venues' current numbers are not the
numbers at a past step.
