# trader

Agentic-first autonomous trading agent. **The LLM proposes, the deterministic RiskGuard
disposes, the broker executes** — the only path to an order runs through pure-Python risk
checks. Greenfield successor to `trading-agent` (v1, archived).

Full design: [`docs/architecture.md`](docs/architecture.md).

## Principles

- Deterministic safety chain — limits are code, never prompt text.
- No market-data storage — fetch-on-tick + a small bars cache for replay.
- One service layer, three surfaces: **REST API + MCP server + CLI**, all over the same
  capability registry (`src/trader/surfaces/services.py`).
- Per-role model routing: local Nemotron for the money-path decide, Grok for research.
- Paper-only, hard-gated; append-only audit for every state-changing action.

## Development

```bash
uv sync --all-extras --dev      # install
uv run pytest -q                # tests
uv run ruff check . && uv run ruff format --check .   # lint

docker compose -f infra/docker-compose.yml up -d db   # Postgres on :5544 (tests need it)
uv run uvicorn trader.surfaces.api.app:app --port 8080   # API → GET /health; MCP at /mcp
trader health                   # CLI surface (talks to the running API)
```

## Deploy

Runs on the DGX Spark beside `nemotron-vllm` (`:8009`).

```bash
docker compose -f infra/docker-compose.yml up -d --build
```

## Status

M0 (foundation / walking skeleton). Milestones tracked in the Linear **trader** project.
