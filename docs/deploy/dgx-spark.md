# Reference deployment: one operator's box (NVIDIA DGX Spark)

This is an **appendix, not a requirement**. Everything the repo needs to run is in
[`docs/runbooks/deploy.md`](../runbooks/deploy.md) (compose or systemd) and
[`infra/`](../../infra). This page records the box the project was developed and
first run on, so that the box-specific facts that used to be scattered through the
docs have one home. None of it is an invariant of the codebase.

## The box

| Fact | Value on this box | Generic equivalent |
|---|---|---|
| Hardware | NVIDIA DGX Spark, 128 GB unified CPU/GPU memory | any Linux host with Docker (or Python 3.12 + uv) |
| Hostname / LAN address | `<host>` / `<lan-ip>` (redacted; the real values live only in the operator's notes) | `<host>` / `<trader-host>` in the docs |
| Repo checkout | `~/repos/trader` (`main`) | `TRADER_REPO`, `%h/repos/trader` in the unit templates |
| `uv` | Homebrew on Linux: `/home/linuxbrew/.linuxbrew/bin/uv` | `%h/.local/bin/uv` (the installer default) or `PATH` |
| Host `psql` | not installed — query through `docker exec` into the compose db | `scripts/desk-watch.sh` handles both |
| v1 archive | `~/repos/trading-agent`, frozen, read-only | not published; its lessons are catalogued in `docs/lessons.md` |
| Box rules file | `~/SPARK_RESOURCES.md` (shared GPU, memory budget, clock cap) | this page |

## The decide model: `nemotron-vllm`

The money-path `decide` role runs on a **local vLLM** serving Nemotron, as a system
`systemd` unit named `nemotron-vllm` listening on `:8009` with **no authentication**
(network trust inside the LAN — it is never exposed beyond it). That is exactly the
Nemotron preset's default, `TRADER_NEMOTRON_BASE_URL=http://localhost:8009/v1`, so on
this box `TRADER_DECIDE_*` stays unset.

- The preset client bakes `chat_template_kwargs: {enable_thinking: false}` into every
  request; without it this endpoint collapses to ~0.35 tok/s
  (`docs/lessons.md#nemotron-thinking-off`). That flag is
  a code invariant of the preset, not a box fact.
- It is a shared 4-sequence instance. Research roles (brief, compiler, critic,
  sentiment) run on Grok partly so the decide loop never queues behind them.
- From inside the compose `app` container the endpoint is
  `http://host.docker.internal:8009/v1` (`extra_hosts: host-gateway`).

## Memory and GPU

vLLM holds most of the 128 GB unified pool; the trader's budget is **~16 GB** for the
API + Postgres together. The compose `app` service carries `mem_limit: 4g` for that
reason — tune it to your host. A **GPU clock-cap systemd unit** on the box is
load-bearing for vLLM stability; it is unrelated to the trader and must not be removed
when touching the host's units.

## Deployed shape on this box

- **Postgres**: the compose `db` service (`restart: unless-stopped`, `:5544`), started
  once with `docker compose -f infra/docker-compose.yml up -d db`.
- **API**: a **systemd user unit** running uvicorn from the checkout, not the compose
  `app` container. The live units are the originals of the templates in
  [`infra/systemd/`](../../infra/systemd): `trader-api.service`,
  `trader-desk-watch.service`, `trader-desk-watch.timer` (hourly at `:23`). They live in
  `~/.config/systemd/user/` with `loginctl enable-linger` set, so they start at boot
  without a login session. The unit binds `0.0.0.0:8080` so LAN clients (Hermes) reach
  the REST API and `/mcp`; auth is mandatory (`TRADER_API_KEY` in `.env`).
- **Deploy**: `git pull` in the checkout, run the suite, `systemctl --user restart trader-api`.
- **Credentials**: Alpaca paper keys and `TRADER_XAI_TOKEN_KEY` in `.env` (never
  committed); the encrypted xAI OAuth lineage at `~/.config/trader/xai_token.enc`.
  Only one process may own that refresh lineage (rotating single-use token) — do not
  run a second OAuth client beside `trader-api`.
- **Desk watcher**: `scripts/desk-watch.sh` appends to `desk-watch.log` in the
  checkout (gitignored) and to the journal (`journalctl --user -t trader-desk-watch`).

## Operational notes that came from this box

- `psql` is absent on the host: `docker exec $(docker ps -qf name=trader-db) psql -U trader -d trader -c "..."`.
- Two replay evals at once hit Alpaca's 429; run them serially.
- The `trader` Postgres role can `CREATE DATABASE`, which is how the test suite makes
  its `<db>_test` sibling and how parallel worktrees get their own databases.
