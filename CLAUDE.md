# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

`trader` — agentic-first autonomous trading agent (v2). Greenfield successor to
`trading-agent` (v1, at `~/repos/trading-agent`, now a frozen reference archive). The full
design is in **`docs/architecture.md`** — read it before non-trivial work.

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
- **Nemotron requests MUST send** `chat_template_kwargs: {enable_thinking: false}` or the
  Spark endpoint collapses to ~0.35 tok/s.
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

Runs on the DGX Spark beside `nemotron-vllm` (systemd, `:8009`, no auth — network trust).
Memory budget ~16 GB (vLLM holds the rest of the 128 GB unified pool). The GPU clock-cap
systemd unit on the Spark is load-bearing — never remove it. `gh` CLI is authenticated.

## Referencing v1

v1 is a preserved archive of hardened lessons — cite it as `v1:<path>`, don't port its shape.
Useful references: `apps/agent/src/trading_agent/engine/sleeve.py` (risk limits),
`engine/signal_monitor.py` (triggers), `adapters/broker/{ibkr,reconcile}.py` (future IBKR
add-back), migration 017 (journal replay columns).

## Issue tracker

GitHub: [sudoTomas/trader/issues](https://github.com/sudoTomas/trader/issues).
Map of remaining work: [`docs/tracker.md`](docs/tracker.md). Historical `EAI-*` in
code is provenance from the Linear era, not the tracker.
