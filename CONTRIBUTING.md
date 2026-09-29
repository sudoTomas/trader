# Contributing

Paper-only. The LLM proposes, the deterministic RiskGuard disposes, the broker
executes. No change may place an order except through that chain. Live Alpaca
client construction must keep refusing unless `TRADER_ALPACA_LIVE_ENABLED=true`.

## Setup

```bash
uv sync --all-extras --dev
docker compose -f infra/docker-compose.yml up -d db   # Postgres on :5544
```

Tests append `_test` to `TRADER_DATABASE_URL` (default
`postgresql://trader:trader@localhost:5544/trader`) and create that database.
Do not point the suite at a database you cannot wipe, and do not weaken the
pins in `tests/conftest.py`. Do not commit `.env`.

Optional hooks (ruff format + ruff check):

```bash
uvx pre-commit install
```

## Gate

Failing test first when the change has behavior. Then, before every commit:

```bash
uv run ruff format . && uv run ruff check . && uv run pytest -q
```

Do not pipe pytest into `tail`; a pipe hides the failing exit. Dates in tests
that touch `authored_at`, `expires_at`, or sessions must be relative to
`datetime.now(tz=UTC)`, not hard-coded.

## Decide-change gate

A prompt edit, a model swap, a new prompt lane, or a risk-chain behavior change
is a decide-behavior change. It goes through
[`docs/runbooks/decide-change-gate.md`](docs/runbooks/decide-change-gate.md):
replay, then a shadow window, then one flip. One gate at a time. RiskGuard
stays the only order door.

## Adding a capability

1. Register the query or command in `src/trader/surfaces/services.py`.
2. Wire the REST API, the MCP server, and the CLI over that registration.
3. Never add the feature to only one surface, and never add a raw place-order tool.

## Commits

`type(scope): imperative summary` — `fix`, `feat`, or `docs`, matching `git log`.
The body says why. A commit that finishes an issue includes `Closes #N`.
