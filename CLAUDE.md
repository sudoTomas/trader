# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

`trader` — agentic-first autonomous trading agent (v2). Greenfield successor to
`trading-agent` (v1, a frozen private archive that is not published with this repo). The
full design is in **`docs/architecture.md`** — read it before non-trivial work.

## Prime directive

The LLM proposes, the deterministic **RiskGuard** disposes, the broker executes. No LLM
output, MCP tool, or API caller may place an order except through the RiskGuard chain. When
in doubt, fail closed.

## Non-negotiable invariants

- **Paper only.** Live client construction must refuse unless `TRADER_ALPACA_LIVE_ENABLED=true`.
- **No market-data storage in the live path.** Fetch-on-tick; compute indicators locally.
  (v1 stored candle windows per row → a 3.6 GB table and 19 GiB loads. Don't.)
- **One service layer, three surfaces.** Add a capability to `surfaces/services.py` first,
  then wire API + MCP + CLI. Never add a feature to only one surface.
- **No LangGraph.** Plain async. Desk fan-out = `asyncio.gather`; HITL = approvals table.
- **Nemotron requests MUST send** `chat_template_kwargs: {enable_thinking: false}` or a
  vLLM-served Nemotron collapses to ~0.35 tok/s. The preset bakes it in; never copy that
  body onto a vendor endpoint (`TRADER_DECIDE_*`).
- **Append-only audit** for every state-changing action; **journal** the context version
  (research/prompt/output) on every decision.

## Commands

```bash
uv sync --all-extras --dev
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
trader health
uv run uvicorn trader.surfaces.api.app:app --port 8080
docker compose -f infra/docker-compose.yml up -d --build
```

## Environment

Deploys as compose (`infra/docker-compose.yml`, the reference) or as a host systemd user
unit over the compose db (`infra/systemd/`); runbook `docs/runbooks/deploy.md`. The decide
model is any OpenAI-compatible endpoint (`TRADER_DECIDE_*`) or the Nemotron preset
(`TRADER_NEMOTRON_BASE_URL`, a self-hosted vLLM; unauthenticated, so LAN only). Nothing
in the repo assumes a particular host; the box it was developed on — a memory-shared GPU
machine — is described in `docs/deploy/dgx-spark.md`, and its constraints (memory cap,
GPU units) are that box's, not invariants. `gh` is the tracker CLI.

## Referencing v1

v1 is a preserved archive of hardened lessons (private, outside this repo). Those lessons
are catalogued in **`docs/lessons.md`**, one slug per lesson; a comment that leans on one
cites it as `see docs/lessons.md#<slug>` rather than a ticket id or an archive path. Don't
port v1's shape. Useful references inside the archive:
`apps/agent/src/trading_agent/engine/sleeve.py` (risk limits), `engine/signal_monitor.py`
(triggers), `adapters/broker/{ibkr,reconcile}.py` (future IBKR add-back), migration 017
(journal replay columns).

## Issue tracker

GitHub: [sudoTomas/trader/issues](https://github.com/sudoTomas/trader/issues).
Map of remaining work: [`docs/tracker.md`](docs/tracker.md). Historical `EAI-*` ids in
the research notes and runbooks are provenance from the Linear era, not the tracker.
