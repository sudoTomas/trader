# Hermes ↔ trader integration guide

How an external agent (Hermes, or any MCP/HTTP client) reads and plugs into the
autonomous trading system running on the DGX Spark (`spark-4b1a`).

## What this system is, in one paragraph

A live-paper trading loop (ETH/USD + SPY on Alpaca paper) that wakes on signals, builds
market context, runs an analyst desk + LLM decider + cross-model critic, passes any
intent through a deterministic RiskGuard, executes, and journals everything. External
agents are **capture clients and readers**: you can feed it research and read its state.
You cannot place orders — no such tool exists on any surface, by design.

## Connectivity

| Surface | Where | Notes |
|---|---|---|
| REST API | `http://192.168.195.251:8080` | LAN address of the Spark; `http://localhost:8080` when on the box |
| MCP server | `http://192.168.195.251:8080/mcp` | Streamable HTTP (FastMCP). Add as a remote MCP server in Hermes |
| Health (no auth ever) | `GET /health` | `{"status":"ok","db":"ok"}` when healthy |

**Auth (enabled 2026-09-10)**: every request — REST and `/mcp` alike — must carry an
`X-API-Key` header matching `TRADER_API_KEY` in `~/repos/trader/.env` on the box
(`/health` is the only exemption). Missing/wrong key → 401. When configuring Hermes's
remote MCP server, set the endpoint to `http://192.168.195.251:8080/mcp/` (note the
trailing slash) with the `X-API-Key` custom header; read the key off the box, never
hard-code it into shared docs.

> History: until 2026-09-10 the service ran auth-disabled AND bound to localhost only —
> a remote client got connection-refused (reported as "no MCP built out"). Both fixed:
> the unit now binds `0.0.0.0` and auth is mandatory (EAI-194).

## The capability surface (identical on MCP, REST, and the `trader` CLI)

### Read tools (safe, use freely)

| MCP tool | REST | What you get |
|---|---|---|
| `get_health` | `GET /health` | liveness + db |
| `get_decisions` / `get_decision` | `GET /decisions[/{id}]` | the journal: every wakeup with prompt, action, desk verdicts, risk verdicts |
| `get_positions` | `GET /positions` | open positions |
| `get_orders` | `GET /orders` | orders, newest first |
| `get_risk_status` | `GET /risk/status` | kill switches, breakers, limits |
| `get_loop_status` | `GET /loop/status` | decide-loop counters and mode |
| `get_macro_view` | `GET /research/view` | current compiled Macro View (the advisory digest your notes feed) |
| `get_research_notes` | `GET /research/notes` | notes the compiler sees (flags widen to stale/retired) |
| `get_brief` | `GET /research/briefs?symbol=` | per-symbol research brief |
| `get_sentiment` | `GET /research/sentiment` | X sentiment polls (scored + honest `unavailable`) |
| `get_eval_runs` / `get_eval_run` | `GET /eval/runs[/{id}]` | replay eval results |
| `get_audit` | `GET /audit` | append-only audit trail |
| `get_approvals` | `GET /risk/approvals` | order intents parked for a human |

### Commands Hermes SHOULD use — research capture (the intended integration)

- **`submit_research_note`** (`POST /research/notes`) — the main event. Distill a
  forwarded article/screenshot/voice memo into an immutable markdown note. Pass
  `authored_at` (ISO-8601 tz-aware, when the research was CREATED) — freshness and
  auto-stale anchor on it; **omit it if unknown, never guess**. Scope with
  `symbols`/`asset_classes`. Full workflow + rules: `skills/research-capture/SKILL.md`
  (install that skill into Hermes).
- `retire_note` — withdraw a note (corrections are a *new* note; notes are immutable).
- `compile_macro_view` / `compile_brief` — force a recompile now (normally debounced
  and automatic on note changes; rarely needed).

Example capture:

```bash
curl -s -X POST http://192.168.195.251:8080/research/notes \
  -H "Content-Type: application/json" \
  -d '{"title":"ETF flows negative 2nd week","body":"<distilled markdown>",
       "symbols":["ETH/USD"],"source":"https://example.com/article",
       "authored_at":"2026-09-09T09:00:00+00:00"}'
```

### Commands external agents must NOT call (operator-only)

`pause`, `resume`, `resume_breaker`, `flatten_all`, `wake_trader`, `resolve_approval`,
`adopt_venue_positions`, `run_replay_eval`, `fetch_x_sentiment` (spends against a daily
cap), `export_research_mirror`. They exist
for the operator's surfaces. `flatten_all` closes every position; treat the whole group
as off-limits unless the operator explicitly directs otherwise in the moment.

## Invariants that keep this safe (do not engineer around them)

1. **No order path exists outside RiskGuard** — there is deliberately no place-order
   tool on MCP/REST. Do not build one.
2. **Research is advisory by construction**: your notes reach the decider's prompt as a
   budgeted lane; RiskGuard (sizing, breakers, limits) can never read them.
3. **Push-only**: you call the trader; the trader never calls out to Hermes. Losing
   Hermes loses convenience, not function.
4. Notes are immutable, auto-stale ~30 days after `authored_at` unless pinned, and every
   capture leaves an audit row.

## Reading the journal usefully

`get_decisions` rows carry: `status` (`shadow` = decided-not-submitted **and** any hold
even in live mode; `executed`/`denied` = a real intent's fate), `decision` (action,
conviction, rationale), `desk_verdicts` (analyst reads + critic verdict + what was
applied), `risk_verdicts`, `macro_view_version` + `brief_version` (which research
versions the decider saw — your note's influence is traceable end to end).

## On-box orientation (if you ssh in)

Repo `~/repos/trader` (work on `main`; **read `HANDOFF.md` first — it is the current
state of truth**). Deployed as systemd user unit `trader-api`; deploy = `git pull` +
`systemctl --user restart trader-api`. Box-level rules (shared GPU, Nemotron endpoint,
memory budget): `~/SPARK_RESOURCES.md`. The frozen v1 archive is `~/repos/trading-agent`
— read-only reference.
